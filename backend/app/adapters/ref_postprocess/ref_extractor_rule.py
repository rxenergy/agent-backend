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

from app.ports.llm import ChatMessage, GrammarSpec, LLMPort

from .ref_resolver import VALID_KINDS, RefResolver, ResolvedRef, build_source_id_filter
from .settings import DEFAULT_MAX_OUTPUT_TOKENS_WITH_FOLLOW_UP, RefSettings


@dataclass
class RawRef:
    raw_citation: str               # 청크에 나타난 원본 인용 텍스트 (감사용)
    kind: str                       # VALID_KINDS 중 하나
    identifier: str                 # 정규화 전 핵심 식별자 (예: "RG 1.68", "10 CFR 50.55a")
    section_path: list[str] = field(default_factory=list)


@dataclass
class FollowUpQuery:
    query_text: str                 # 재검색용 쿼리 문자열
    target_source_ids: list[str]    # resolver가 매핑한 source_id 리스트 (OpenSearch 필터용)
    intent: str = ""                # 어떤 의도 측면을 담는지 요약


# vLLM guided_json / OpenAI response_format용 출력 스키마
RESOLVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_citation": {"type": "string"},
                    "kind": {"type": "string", "enum": list(VALID_KINDS)},
                    "identifier": {"type": "string"},
                    "section_path": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["raw_citation", "kind", "identifier"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["references"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You extract citations to OTHER documents from a chunk of U.S. NRC \
regulatory text. You DO NOT resolve them to any database — only list what is cited.

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
    TR    NuScale Topical/Technical Report  e.g. "TR-0516-49416", "NP-TR-0610-289-NP",
                                            "Topical Report TR-0915-17565, Rev 0", "TR-102621"
    FSAR  NuScale FSAR (chapter/tier)       e.g. "FSAR Chapter 15", "FSAR Ch. 19", "FSAR Tier 2"
    RAI   NuScale Request for Additional Information  e.g. "RAI 8932", "RAI 5452",
                                            "RAI 8932 Question 03.07.02-6"
    SECY  NRC SECY / SRM paper    e.g. "SECY-19-0079", "SECY-94-084", "SRM-SECY-19-0036"
    OTHER any external standard not above (ASME, IEEE, ANS, ...)
- identifier: the core identifier only (e.g. "RG 1.68", "10 CFR 50.55a", "TR-0516-49416").
    For FSAR use "FSAR" (chapter goes in section_path). For RAI use "RAI <number>".
- section_path: optional sub-location inside the cited doc.
    e.g. ["Part 50", "50.55a"]; for FSAR ["Chapter 15"]; for RAI the question number ["03.07.02-6"].

Rules:
1. Only cite SEPARATE documents. Skip bare pointers like "see Section 3.2" with no doc name.
2. Never include the current document itself.
3. Deduplicate identical citations.
4. If nothing is cited, return {"references": []}.
Return ONLY the JSON object matching the schema. No prose."""


async def extract_raw_refs(
    *,
    text: str,
    settings: RefSettings,
    llm: LLMPort,
    current_source_id: str | None = None,
) -> list[RawRef]:
    """청크/질의 텍스트에서 raw 참조 리스트를 단일 LLM 호출로 추출.

    LLM 호출은 주입된 :class:`LLMPort` 를 통해 수행한다(어댑터가 vLLM guided
    decoding 으로 변환). ``settings`` 는 max_tokens·served_model_name 등 추출 knob
    제공에만 쓰인다(연결 정보는 LLMPort 가 소유).
    """
    user = text if not current_source_id else f"current_source_id: {current_source_id}\n\n{text}"

    result = await llm.generate_messages(
        [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="user", content=user),
        ],
        model_options={"max_tokens": settings.max_output_tokens, "temperature": 0.0},
        grammar=GrammarSpec(kind="json_schema", value=RESOLVE_SCHEMA),
    )
    content = result.text or "{}"
    return _parse_raw_refs(content, current_source_id)


def _parse_raw_refs(content: str, current_source_id: str | None) -> list[RawRef]:
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
        if not (identifier or raw_citation) or key in seen:
            continue
        seen.add(key)
        refs.append(RawRef(
            raw_citation=raw_citation or identifier, kind=kind,
            identifier=identifier or raw_citation, section_path=section_path,
        ))
    return refs


async def resolve_text(
    *,
    text: str,
    resolver: RefResolver,
    settings: RefSettings,
    llm: LLMPort,
    current_source_id: str | None = None,
    min_score: float = 0.6,
) -> dict:
    """질의 시점 진입점: 추출(LLM) → 해소(rule-base) → OpenSearch 필터.

    반환: {"raw_refs": [...], "resolved": [ResolvedRef...], "filter": {...}|None}
    """
    raw_refs = await extract_raw_refs(
        text=text, settings=settings, llm=llm, current_source_id=current_source_id,
    )
    resolved = resolver.resolve_many(raw_refs)
    return {
        "raw_refs": raw_refs,
        "resolved": resolved,
        "filter": build_source_id_filter(resolved, min_score=min_score),
    }


# ---------------------------------------------------------------------------
# Follow-up query 생성 확장
# ---------------------------------------------------------------------------

RESOLVE_WITH_FOLLOW_UP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "references": RESOLVE_SCHEMA["properties"]["references"],
        "follow_up_queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query_text": {"type": "string"},
                    "target_identifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "intent": {"type": "string"},
                },
                "required": ["query_text", "target_identifiers"],
                "additionalProperties": False,
            },
            "maxItems": 5,
        },
    },
    "required": ["references", "follow_up_queries"],
    "additionalProperties": False,
}


SYSTEM_PROMPT_WITH_FOLLOW_UP = """\
You extract citations to OTHER documents from a chunk of U.S. NRC regulatory text, \
AND generate follow-up search queries based on the user's original search intent.

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

Rules:
1. Only cite SEPARATE documents. Skip bare pointers like "see Section 3.2" with no doc name.
2. Never include the current document itself.
3. Deduplicate identical citations.
4. If nothing is cited, return {"references": [], "follow_up_queries": []}.

## Part 2: Follow-up Query Generation

You are given:
- ORIGINAL USER QUERY: what the user originally wanted to know.
- RETRIEVED CHUNK: a text chunk from the initial search results.

After extracting references, generate 1-5 follow-up search queries that:
1. Capture specific aspects of the ORIGINAL USER QUERY's intent.
2. Are reformulated to search WITHIN the referenced documents.
3. Use NRC domain terminology (English) appropriate for the target documents.
4. Are specific enough to retrieve relevant passages (10-40 words each).

For each follow-up query:
- query_text: the search query string.
- target_identifiers: which identifiers from your references list this query targets.
- intent: (optional) brief note on what aspect of user intent this addresses.

Rules for follow-up queries:
1. Only target documents that appear in your references list.
2. If no references are found, return an empty follow_up_queries array.
3. Each query should target a different aspect of the user's information need.
4. Avoid generic queries — be specific to the user's question angle.

Return ONLY the JSON object matching the schema. No prose."""


async def extract_refs_with_follow_up(
    *,
    query_text: str,
    chunk_text: str,
    settings: RefSettings,
    llm: LLMPort,
    current_source_id: str | None = None,
) -> tuple[list[RawRef], list[dict]]:
    """참조 추출 + follow-up 쿼리 생성을 단일 LLM 호출로 수행.

    반환: (raw_refs, raw_follow_ups)
    raw_follow_ups는 LLM 원본 dict 리스트 (target_identifiers 포함).
    """
    user_parts = [f"ORIGINAL USER QUERY: {query_text}", ""]
    if current_source_id:
        user_parts.append(f"current_source_id: {current_source_id}")
        user_parts.append("")
    user_parts.append(f"RETRIEVED CHUNK:\n{chunk_text}")
    user_content = "\n".join(user_parts)

    result = await llm.generate_messages(
        [
            ChatMessage(role="system", content=SYSTEM_PROMPT_WITH_FOLLOW_UP),
            ChatMessage(role="user", content=user_content),
        ],
        model_options={
            "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS_WITH_FOLLOW_UP,
            "temperature": 0.0,
        },
        grammar=GrammarSpec(kind="json_schema", value=RESOLVE_WITH_FOLLOW_UP_SCHEMA),
    )
    content = result.text or "{}"
    return _parse_refs_and_follow_ups(content, current_source_id)


def _parse_refs_and_follow_ups(
    content: str, current_source_id: str | None
) -> tuple[list[RawRef], list[dict]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [], []

    raw_refs = _parse_raw_refs(content, current_source_id)

    follow_ups: list[dict] = []
    if isinstance(data, dict):
        for item in data.get("follow_up_queries", []):
            if not isinstance(item, dict):
                continue
            qt = str(item.get("query_text", "")).strip()
            if not qt:
                continue
            targets = item.get("target_identifiers") or []
            if isinstance(targets, list):
                targets = [str(t).strip() for t in targets if str(t).strip()]
            else:
                targets = []
            intent = str(item.get("intent", "")).strip()
            follow_ups.append({
                "query_text": qt,
                "target_identifiers": targets,
                "intent": intent,
            })
    return raw_refs, follow_ups



def _resolve_follow_up_targets(
    raw_follow_ups: list[dict],
    raw_refs: list[RawRef],
    resolved: list[ResolvedRef],
    min_score: float,
) -> list[FollowUpQuery]:
    """LLM이 출력한 target_identifiers를 source_id로 매핑."""
    # identifier → source_ids 매핑 구축
    ident_to_sids: dict[str, list[str]] = {}
    for raw_ref, res_ref in zip(raw_refs, resolved):
        sids = [c.source_id for c in res_ref.candidates if c.score >= min_score]
        if sids:
            ident_to_sids[raw_ref.identifier.strip().upper()] = sids
            ident_to_sids[raw_ref.raw_citation.strip().upper()] = sids

    result: list[FollowUpQuery] = []
    for fq in raw_follow_ups:
        target_sids: list[str] = []
        seen: set[str] = set()
        for ident in fq["target_identifiers"]:
            key = ident.strip().upper()
            for sid in ident_to_sids.get(key, []):
                if sid not in seen:
                    seen.add(sid)
                    target_sids.append(sid)
        if not target_sids:
            continue
        result.append(FollowUpQuery(
            query_text=fq["query_text"],
            target_source_ids=target_sids,
            intent=fq.get("intent", ""),
        ))
    return result


async def resolve_text_with_follow_up(
    *,
    query_text: str,
    chunk_text: str,
    resolver: RefResolver,
    settings: RefSettings,
    llm: LLMPort,
    current_source_id: str | None = None,
    min_score: float = 0.6,
) -> dict:
    """검색 에이전트 진입점: 참조 추출 + 해소 + follow-up 쿼리(source_id 매핑 완료).

    반환: {
        "raw_refs": list[RawRef],
        "resolved": list[ResolvedRef],
        "filter": {"terms": {"source_id": [...]}} | None,
        "follow_up_queries": list[FollowUpQuery],
    }
    """
    raw_refs, raw_follow_ups = await extract_refs_with_follow_up(
        query_text=query_text,
        chunk_text=chunk_text,
        settings=settings,
        llm=llm,
        current_source_id=current_source_id,
    )
    resolved = resolver.resolve_many(raw_refs)
    follow_up_queries = _resolve_follow_up_targets(
        raw_follow_ups, raw_refs, resolved, min_score,
    )
    return {
        "raw_refs": raw_refs,
        "resolved": resolved,
        "filter": build_source_id_filter(resolved, min_score=min_score),
        "follow_up_queries": follow_up_queries,
    }
