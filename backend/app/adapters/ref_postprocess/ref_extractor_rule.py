"""1차 추출기 — vLLM(gemma-4-26b)을 **단일 호출 structured output**으로 사용.

기존 v2(tool-calling 32턴 루프)와 달리, LLM은 **참조된 외부 문서를 raw 리스트로
뽑는 일만** 한다(퍼지 인식). catalog 매핑(해소)은 :mod:`ref_resolver`의 rule-base가
결정적으로 처리한다.

LLM 호출은 :class:`~app.ports.llm.LLMPort` 의 ``generate_messages`` (system+user
메시지 + ``GrammarSpec(kind="json_schema")`` guided decoding)로 1회만 수행한다.
어댑터(HttpLLM)가 vLLM 의 ``guided_json``/``response_format`` 으로 변환하므로 이
모듈은 외부 LLM SDK 를 직접 import 하지 않는다(원칙 #4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import ChatMessage, GrammarSpec, LLMPort

from .ref_resolver import VALID_KINDS, RefResolver, ResolvedRef, build_source_id_filter
from .settings import DEFAULT_MAX_OUTPUT_TOKENS_WITH_FOLLOW_UP, RefSettings

_log = structlog.get_logger("ref.follow_up")

# follow-up 참조 추출 LLM 호출을 Phoenix 에 다른 LLM 노드와 동형으로 노출하기 위한
# tracer. 어댑터(HttpLLM)는 자체 span 을 내지 않으므로, 생성 노드(spec_driven _generate
# 의 llm.generation span)와 마찬가지로 호출자가 LLM span 을 씌워 모델 입출력을 싣는다.
_TRACER = get_tracer("agent")


@dataclass
class RawRef:
    raw_citation: str               # 청크에 나타난 원본 인용 텍스트 (감사용)
    kind: str                       # VALID_KINDS 중 하나
    identifier: str                 # 정규화 전 핵심 식별자 (예: "RG 1.68", "10 CFR 50.55a")
    section_path: list[str] = field(default_factory=list)
    # 방안 B — 이 참조 문서를 더 깊이 검색해야 할 때 LLM 이 inline 으로 붙이는 재검색 쿼리
    # ({"query_text", "intent"}). 불필요하면 None. resolver/필터는 이 필드를 읽지 않으므로
    # (raw_citation/kind/identifier/section_path 만 소비) 추가돼도 해소에 무해하다.
    follow_up_query: dict | None = None


@dataclass
class FollowUpQuery:
    query_text: str                 # 재검색용 쿼리 문자열
    target_source_ids: list[str]    # resolver가 매핑한 source_id 리스트 (OpenSearch 필터용)
    intent: str = ""                # 어떤 의도 측면을 담는지 요약


# vLLM guided_json / OpenAI response_format용 출력 스키마.
# 방안 B — 각 reference 가 자기 follow_up_query(옵셔널)를 inline 으로 소유한다. 평행
# 두 배열(references + follow_up_queries)을 target_identifiers 문자열로 join 하던
# 구조를 폐기 → reference↔follow_up 이 구조적으로 1:1 결합되어 식별자 불일치/미등록
# silent-drop 이 원천 소멸한다.
_FOLLOW_UP_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query_text": {"type": "string"},
        "intent": {"type": "string"},
    },
    "required": ["query_text"],
    "additionalProperties": False,
}

_REFERENCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "raw_citation": {"type": "string"},
        "kind": {"type": "string", "enum": list(VALID_KINDS)},
        "identifier": {"type": "string"},
        "section_path": {"type": "array", "items": {"type": "string"}},
        # 옵셔널 — 이 참조 문서를 더 깊이 검색해야 할 때만 붙는다. 없으면 follow-up 없음.
        "follow_up_query": _FOLLOW_UP_QUERY_SCHEMA,
    },
    "required": ["raw_citation", "kind", "identifier"],
    "additionalProperties": False,
}


def _parse_raw_refs(content: str, current_source_id: str | None) -> list[RawRef]:
    """LLM JSON(references 배열) → list[RawRef]. 방안 B — 각 reference 의 옵셔널
    follow_up_query 를 방어적으로 파싱해 그 RawRef 에 실어둔다(이후 _build_follow_ups 가
    이 청크의 해소 source_id 와 결합). vLLM guided_json 이 옵셔널 객체를 absent/null/{}
    어느 형태로 내든 모두 관용한다(query_text 비면 None)."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    refs: list[RawRef] = []
    seen: set[tuple[str, str]] = set()
    for item in data.get("references", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().upper()
        if kind not in VALID_KINDS:
            kind = "OTHER"
        identifier = str(item.get("identifier", "")).strip()
        raw_citation = str(item.get("raw_citation", "")).strip()
        sp = item.get("section_path") or []
        section_path = [str(x).strip() for x in sp if str(x).strip()] if isinstance(sp, list) else []
        key = (kind, identifier.upper())
        # dedup — 같은 (kind, identifier) 가 두 번 나오면 둘째는 그 follow_up_query 와 함께
        # drop 된다(살아남은 첫 ref 가 자기 follow_up 을 보유하므로 의도와 일치).
        if not (identifier or raw_citation) or key in seen:
            continue
        seen.add(key)
        refs.append(RawRef(
            raw_citation=raw_citation or identifier, kind=kind,
            identifier=identifier or raw_citation, section_path=section_path,
            follow_up_query=_parse_follow_up_query(item.get("follow_up_query")),
        ))
    return refs


def _parse_follow_up_query(raw: Any) -> dict | None:
    """reference 에 inline 된 follow_up_query 를 방어적으로 정규화. dict 이고 비어있지
    않은 query_text 를 가질 때만 {"query_text", "intent"} 를 돌려준다(아니면 None)."""
    if not isinstance(raw, dict):
        return None
    qt = str(raw.get("query_text", "")).strip()
    if not qt:
        return None
    return {"query_text": qt, "intent": str(raw.get("intent", "")).strip()}


# ---------------------------------------------------------------------------
# Follow-up query 생성 확장
# ---------------------------------------------------------------------------

# 방안 B — 단일 references 배열. 각 reference 가 자기 follow_up_query(옵셔널)를 소유한다.
# 구 maxItems:5 는 follow_up_queries 에 있었으나, 이제 follow-up 이 reference 에 조건부로
# 붙으므로 references 자체를 8 로 캡한다(구 5 follow-up 보다 약간 높임 — 모든 reference 가
# follow-up 을 갖지는 않으므로). 엄격한 ≤5 follow-up 캡이 필요하면 _build_follow_ups 에서.
RESOLVE_WITH_FOLLOW_UP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "references": {
            "type": "array",
            "items": _REFERENCE_ITEM_SCHEMA,
            "maxItems": 8,
        },
    },
    "required": ["references"],
    "additionalProperties": False,
}


SYSTEM_PROMPT_WITH_FOLLOW_UP = """\
You extract citations to OTHER documents from a chunk of U.S. NRC regulatory text, \
AND, for references that should be searched deeper, attach a follow-up search query \
INLINE on that reference based on the user's original search intent.

## Part 1: Reference Extraction

For each citation to a SEPARATE document, output one entry with:
- raw_citation: the exact text as it appears in the chunk (verbatim, for audit).
- kind: one of
    RG    Regulatory Guide        e.g. "RG 1.68", "Regulatory Guide 1.68"
    NUREG NUREG / NUREG-CR        e.g. "NUREG-0800", "NUREG/CR-6909"
    FR    Federal Register        e.g. "81 FR 88719", docket "NRC-2016-0248"
    SRP   Standard Review Plan    e.g. "SRP Section 3.2.2"
    DSRS  Design-Specific Review Standard e.g. "NuScale DSRS Section 10.3"
    CFR   Code of Federal Regs    e.g. "10 CFR 50.55a", "10 CFR Part 50"
    GDC   General Design Criterion e.g. "GDC 4", "Criterion 4"
    ML    ADAMS accession         e.g. "ML15355A513"
    TR    NuScale Topical/Technical Report  e.g. "TR-0516-49416"
    FSAR  NuScale FSAR (chapter/tier)       e.g. "FSAR Chapter 15"
    RAI   NuScale Request for Additional Information  e.g. "RAI 8932"
    SECY  NRC SECY / SRM paper    e.g. "SECY-19-0079"
    OTHER any external standard not above (ASME, IEEE, ANS, ...)
- identifier: the core identifier only (e.g. "RG 1.68", "10 CFR 50.55a").
    For FSAR use "FSAR" (chapter goes in section_path). For RAI use "RAI <number>".
- section_path: optional sub-location inside the cited doc.
- follow_up_query: (optional) if THIS referenced document should be searched deeper to \
satisfy the user's intent, attach an object {query_text, intent}. Omit it entirely for \
references that need no follow-up search.

Rules:
1. Only cite SEPARATE documents. Skip bare pointers like "see Section 3.2" with no doc name.
2. Never include the current document itself.
3. Deduplicate identical citations.
4. If nothing is cited, return {"references": []}.

## Part 2: Follow-up Query Generation (inline, per reference)

You are given:
- ORIGINAL USER QUERY: what the user originally wanted to know.
- RETRIEVED CHUNK: a text chunk from the initial search results.

For each reference that warrants deeper search, attach a follow_up_query whose query_text:
1. Captures specific aspects of the ORIGINAL USER QUERY's intent.
2. Is reformulated to search WITHIN that referenced document.
3. Uses NRC domain terminology (English) appropriate for the target document.
4. Is specific enough to retrieve relevant passages (10-40 words).

For each follow_up_query:
- query_text: the search query string.
- intent: (optional) brief note on what aspect of user intent this addresses.

Rules for follow-up queries:
1. Attach a follow_up_query only to references genuinely worth searching deeper.
2. Different references' queries should target different aspects of the user's need.
3. Avoid generic queries — be specific to the user's question angle.

Return ONLY the JSON object matching the schema. No prose."""


# spec_driven_v2 N3.5 고도화 — answer_spec + 슬롯 검색 쿼리를 함께 받아, 청크의 *모든*
# 외부 참조를 뽑는 대신 "사용자 질문에 답하는 데 꼭 필요한" 참조만 선별한다. Part 1 의
# 추출 규칙(kind/identifier/section_path)은 동일하되, 필요-판정 게이트가 추가된다.
SYSTEM_PROMPT_NECESSITY = """\
You extract citations to OTHER documents from a chunk of U.S. NRC regulatory text, \
AND generate follow-up search queries — but ONLY for references that are genuinely \
NEEDED to answer the user's question for the current answer slot.

## Part 1: Necessity-judged Reference Extraction

You are given:
- USER QUESTION: what the user originally wanted to know.
- ANSWER SPEC: the evidence the answer must rest on (intent, structure, governing authority, the required slot).
- SLOT SEARCH QUERY: the query that retrieved this chunk for the current slot.
- SEARCH DIRECTION (optional): guidance from the verification step on what to look for, and from which angle, when searching the external document this chunk cites.
- RETRIEVED CHUNK: a text chunk from the initial search results.

For each citation to a SEPARATE document, output one entry with:
- raw_citation: the exact text as it appears in the chunk (verbatim, for audit).
- kind: one of
    RG    Regulatory Guide        e.g. "RG 1.68", "Regulatory Guide 1.68"
    NUREG NUREG / NUREG-CR        e.g. "NUREG-0800", "NUREG/CR-6909"
    FR    Federal Register        e.g. "81 FR 88719", docket "NRC-2016-0248"
    SRP   Standard Review Plan    e.g. "SRP Section 3.2.2"
    DSRS  Design-Specific Review Standard e.g. "NuScale DSRS Section 10.3"
    CFR   Code of Federal Regs    e.g. "10 CFR 50.55a", "10 CFR Part 50"
    GDC   General Design Criterion e.g. "GDC 4", "Criterion 4"
    ML    ADAMS accession         e.g. "ML15355A513"
    TR    NuScale Topical/Technical Report  e.g. "TR-0516-49416"
    FSAR  NuScale FSAR (chapter/tier)       e.g. "FSAR Chapter 15"
    RAI   NuScale Request for Additional Information  e.g. "RAI 8932"
    SECY  NRC SECY / SRM paper    e.g. "SECY-19-0079"
    OTHER any external standard not above (ASME, IEEE, ANS, ...)
- identifier: the core identifier only (e.g. "RG 1.68", "10 CFR 50.55a").
    For FSAR use "FSAR" (chapter goes in section_path). For RAI use "RAI <number>".
- section_path: optional sub-location inside the cited doc.
- follow_up_query: (optional) for a NEEDED reference, attach an object {query_text, intent} \
reformulated to search WITHIN that referenced document. Omit it for references that need no \
deeper search.

NECESSITY RULE (the key difference): do NOT list every cited document. Include a \
reference ONLY if searching it is genuinely needed to answer the USER QUESTION for \
this slot (per the ANSWER SPEC) — i.e. the chunk defers a fact the answer requires to \
that other document. Skip references that are merely mentioned, tangential, or whose \
content is already sufficient in this chunk. Fewer, decision-relevant references are better.

Other rules:
1. Only cite SEPARATE documents. Skip bare pointers like "see Section 3.2" with no doc name.
2. Never include the current document itself.
3. Deduplicate identical citations.
4. If no reference is NEEDED, return {"references": []}.

## Part 2: Follow-up Query Generation (inline, per NEEDED reference)

For each NEEDED reference, attach a follow_up_query reformulated to search WITHIN that \
referenced document, in NRC domain English terminology, specific to the user's question \
angle (10-40 words). For each follow_up_query:
- query_text: the search query string.
- intent: (optional) brief note on what aspect of the user's need this addresses.

Rules for follow-up queries:
1. Attach a follow_up_query only to NEEDED references (those in your necessity-filtered list).
2. Be specific to the user's question angle — avoid generic queries.
3. If a SEARCH DIRECTION is provided, treat it as the primary steer — phrase the \
reference's follow_up_query.query_text to look for exactly what the SEARCH DIRECTION asks, \
in the cited document.

Return ONLY the JSON object matching the schema. No prose."""


async def extract_refs_with_follow_up(
    *,
    query_text: str,
    chunk_text: str,
    settings: RefSettings,
    llm: LLMPort,
    current_source_id: str | None = None,
    answer_spec: str | None = None,
    slot_query: str | None = None,
    necessity_only: bool = False,
    search_direction: str | None = None,
) -> list[RawRef]:
    """참조 추출 + follow-up 쿼리 생성을 단일 LLM 호출로 수행.

    `answer_spec`/`slot_query`/`necessity_only` 는 spec_driven_v2 N3.5 고도화용(옵셔널).
    `necessity_only=True` 면 SYSTEM_PROMPT_NECESSITY 로 "답변에 꼭 필요한" 참조만 선별하고,
    user content 에 ANSWER SPEC·SLOT SEARCH QUERY 블록을 싣는다. 미지정 시 기존 동작
    (전체 추출, SYSTEM_PROMPT_WITH_FOLLOW_UP)과 byte-identical.

    `search_direction` 은 verify_slot 이 이 멀티홉 청크에 부여한 재검색 방향(1문장, 옵셔널 —
    necessity_only 경로에서만 의미). 주어지면 user content 에 SEARCH DIRECTION 블록을 실어
    재검색 쿼리가 이 방향을 우선 반영하게 한다. None → 기존 동작과 byte-identical.

    반환: list[RawRef] — 방안 B 로 각 RawRef 가 자기 follow_up_query(옵셔널)를 inline 으로
    소유한다(평행 follow_up_queries 배열 폐기). 해소+follow-up 결합은 호출부가 한다.
    """
    if necessity_only:
        system_prompt = SYSTEM_PROMPT_NECESSITY
        user_parts = [f"USER QUESTION: {query_text}", ""]
        if answer_spec:
            user_parts.append("ANSWER SPEC:")
            user_parts.append(answer_spec)
            user_parts.append("")
        if slot_query:
            user_parts.append(f"SLOT SEARCH QUERY: {slot_query}")
            user_parts.append("")
        if search_direction:
            user_parts.append(f"SEARCH DIRECTION: {search_direction}")
            user_parts.append("")
    else:
        system_prompt = SYSTEM_PROMPT_WITH_FOLLOW_UP
        user_parts = [f"ORIGINAL USER QUERY: {query_text}", ""]
    if current_source_id:
        user_parts.append(f"current_source_id: {current_source_id}")
        user_parts.append("")
    user_parts.append(f"RETRIEVED CHUNK:\n{chunk_text}")
    user_content = "\n".join(user_parts)

    # follow-up 추출 LLM 호출을 LLM-kind span 으로 — 다른 노드처럼 Phoenix 에서 모델
    # 입출력(system+user 메시지, 생성된 JSON)을 그대로 확인할 수 있게 한다. 청크별로
    # 1개씩 떠 retrieval.follow_up tool span 아래에 nesting 된다(질의·current_source_id
    # 속성으로 어느 청크 추출인지 식별).
    with _TRACER.start_as_current_span("llm.follow_up_extract") as s:
        oi.set_kind(s, oi.KIND_LLM)
        if current_source_id:
            s.set_attribute("follow_up.current_source_id", current_source_id)
        result = await llm.generate_messages(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_content),
            ],
            model_options={
                "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS_WITH_FOLLOW_UP,
                "temperature": 0.0,
            },
            grammar=GrammarSpec(kind="json_schema", value=RESOLVE_WITH_FOLLOW_UP_SCHEMA),
        )
        content = result.text or "{}"
        oi.set_llm_chat(
            s,
            model_name=result.model_id,
            input_messages=[
                ("system", system_prompt),
                ("user", user_content),
            ],
            completion=content,
            prompt_tokens=int(result.token_usage.get("prompt_tokens", 0)),
            completion_tokens=int(result.token_usage.get("completion_tokens", 0)),
        )
    return _parse_raw_refs(content, current_source_id)


def _build_follow_ups(
    raw_refs: list[RawRef],
    resolved: list[ResolvedRef],
    min_score: float,
) -> list[FollowUpQuery]:
    """방안 B — reference 가 inline 으로 소유한 follow_up_query 를 그 reference 의 해소
    source_id 와 결합한다(per-reference walk). 구 _resolve_follow_up_targets 의
    target_identifiers 문자열 cross-join 을 폐기 → 식별자 불일치/미등록 silent-drop 소멸.

    NOTE: resolver.resolve_many(ref_resolver.py)가 입력 순서를 *보존*하므로 zip 인덱스
    정렬이 reference↔follow_up 결합의 불변식이다. resolve_many 가 reorder/filter 하면 깨짐.
    """
    out: list[FollowUpQuery] = []
    dropped = 0
    for raw_ref, res_ref in zip(raw_refs, resolved):
        fq = raw_ref.follow_up_query
        if not fq:
            continue  # follow-up 없음 — drop 아님, 의도된 부재
        sids: list[str] = []
        seen: set[str] = set()
        for c in res_ref.candidates:
            if c.score >= min_score and c.source_id not in seen:
                seen.add(c.source_id)
                sids.append(c.source_id)
        if not sids:
            # 허용되는 drop: 이 reference 가 min_score 위로 해소되지 않음(인덱스 미스/저점수).
            # 식별자 매칭 실패가 아니라 "참조를 인덱스에서 못 찾음" — 로그로 가시화한다.
            dropped += 1
            continue
        out.append(FollowUpQuery(
            query_text=fq["query_text"],
            target_source_ids=sids,
            intent=fq.get("intent", ""),
        ))
    if dropped:
        _log.info("follow_up_dropped_unresolved",
                  dropped=dropped, kept=len(out), min_score=min_score)
    return out


async def resolve_text_with_follow_up(
    *,
    query_text: str,
    chunk_text: str,
    resolver: RefResolver,
    settings: RefSettings,
    llm: LLMPort,
    current_source_id: str | None = None,
    min_score: float = 0.6,
    answer_spec: str | None = None,
    slot_query: str | None = None,
    necessity_only: bool = False,
    search_direction: str | None = None,
) -> dict:
    """검색 에이전트 진입점: 참조 추출 + 해소 + follow-up 쿼리(source_id 매핑 완료).

    `answer_spec`/`slot_query`/`necessity_only`/`search_direction` 은 spec_driven_v2 N3.5
    고도화용(옵셔널 — extract_refs_with_follow_up 으로 전달). 미지정 시 기존 동작과 동일.

    반환: {
        "raw_refs": list[RawRef],
        "resolved": list[ResolvedRef],
        "filter": {"terms": {"source_id": [...]}} | None,
        "follow_up_queries": list[FollowUpQuery],
    }
    """
    raw_refs = await extract_refs_with_follow_up(
        query_text=query_text,
        chunk_text=chunk_text,
        settings=settings,
        llm=llm,
        current_source_id=current_source_id,
        answer_spec=answer_spec,
        slot_query=slot_query,
        necessity_only=necessity_only,
        search_direction=search_direction,
    )
    resolved = resolver.resolve_many(raw_refs)
    follow_up_queries = _build_follow_ups(raw_refs, resolved, min_score)
    return {
        "raw_refs": raw_refs,
        "resolved": resolved,
        "filter": build_source_id_filter(resolved, min_score=min_score),
        "follow_up_queries": follow_up_queries,
    }
