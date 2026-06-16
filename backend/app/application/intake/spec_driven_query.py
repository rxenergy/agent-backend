from __future__ import annotations

import json
import re
from typing import Any

from app.application.agents.events import LazyReasoning, current_emitter
from app.application.intake.reasoning_capture import extract_reasoning, stream_capture
from app.domain.spec_driven import AnswerSpec, FormulatedQuery
from app.observability import openinference as oi
from app.observability.logging import get_logger
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort, LLMUnavailableError

_TRACER = get_tracer("intake")
_LOG = get_logger("intake.spec_driven_query")

# spec_driven_v1 N2 — Query Formulation Node. Answer Spec 의 슬롯·명시적 참조를 *구체
# 검색쿼리*(슬롯당 1개)로 옮긴다(설계 §3.2). 리터럴 키워드 보존 + 명시적 참조 verbatim
# 합류(BM25 lexical 앵커) + collection boost(가산만, hard filter 아님 — 사용자 #2).
#
# 모델(utility LLM + json_schema)이 쿼리를 *표현*하고, 결정론 layer 가 두 안전망을 건다:
#   (1) 모든 explicit_reference 가 ≥1 쿼리의 query_text 에 verbatim 으로 들어가게 보장
#       (모델이 슬롯 매핑을 놓쳐도 lexical 앵커가 유실되지 않게 — 본 variant 의 load-bearing
#       신호). (2) reference prefix 에서 collection boost 를 결정론적으로 유도(모델 누락 보정).
#
# 프롬프트·스키마는 registry(spec_driven_query_prompts)에서 SpecDrivenQuerySource 주입.

# 허용 collection 값 — corpus_map collections + nuscale_* 문서군(nrc-all-v1 keyword 필드).
# boost(target) 와 filter(filters) 양쪽 모드가 이 집합으로 검증된다.
_COLLECTIONS = frozenset({
    "10CFR", "DSRS", "FR", "RG", "SRP",
    "nuscale_Affidavit", "nuscale_Audit", "nuscale_DCA", "nuscale_etc", "nuscale_FSAR",
    "nuscale_Inspection", "nuscale_Letter", "nuscale_Meeting", "nuscale_RAI",
    "nuscale_SER", "nuscale_TechReport", "nuscale_Topical_Report",
})

# reference 토큰 → collection 유도(결정론, 대소문자 무시). GDC/Appendix 는 10 CFR 50 의
# 일부이므로 10CFR, NUREG-0800 은 SRP 의 문서번호.
_COLLECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("10 cfr", "10CFR"),
    ("10cfr", "10CFR"),
    ("gdc", "10CFR"),
    ("appendix", "10CFR"),
    ("app k", "10CFR"),
    ("app b", "10CFR"),
    ("nureg-0800", "SRP"),
    ("srp", "SRP"),
    ("dsrs", "DSRS"),
    ("federal register", "FR"),
    ("reg guide", "RG"),
    ("rg ", "RG"),
    ("rg-", "RG"),
)


def _derive_collection(text: str) -> str | None:
    low = text.lower()
    for needle, coll in _COLLECTION_PATTERNS:
        if needle in low:
            return coll
    return None


# === 검색 스코프 메타데이터 채널 (설계 spec_driven_search_scope_metadata.design.v1) ===
# collection 외 status/design/canonical_id 를 N3 retrieval.search 의 filters/target 에
# *인덱스 필드 경로* 키로 싣는다. _opensearch_hybrid 가 임의 필드명을 term/terms 로
# 변환하므로 DSL 빌더 수정 없이 동작한다. status↔규제 / design↔NuScale 배타성(§0-C)을
# 코드가 강제한다: 부적합 collection 슬롯에 실린 채널은 *무시*(빈값 필터 → 0건 방지).
_STATUS_FIELD = "doc_metadata.std_status.keyword"
_DESIGN_FIELD = "doc_metadata.std_design.keyword"
_CANONICAL_FIELD = "doc_metadata.std_canonical_id.keyword"

# std_status 는 RG/SRP/DSRS 만 보유(10CFR/FR/nuscale_* 빈값).
_STATUS_VALUES = frozenset({
    "current", "history", "draft", "withdrawn", "AdditionalInformation",
})
_STATUS_COLLECTIONS = frozenset({"RG", "SRP", "DSRS"})

# std_design 은 nuscale_* 만 보유. 값 표기는 인덱스 적재 표기(언더스코어 없음 — 실측
# 확인: agg 결과 US600/US460/PreApp). PreApp=Pre-Application 단계 문서.
_DESIGN_VALUES = frozenset({"US460", "US600", "PreApp"})
_DESIGN_COLLECTION_PREFIX = "nuscale_"

# canonical_id 정규화 가능 형식(NRC_MANUAL 한정 — 데이터 설명 "canonical ID 규칙").
# doc_type prefix → (정규식, collection). 검증 통과 시에만 스코프로 승격.
_CANONICAL_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("RG", re.compile(r"^RG-\d+\.\d+$")),
    ("SRP", re.compile(r"^SRP-\d+([.\-].+)?$")),
    ("DSRS", re.compile(r"^DSRS-\d+(\.\d+)*$")),
    ("10CFR", re.compile(r"^10CFR-Part[\w\-]+$")),
)


def _validate_canonical_id(cid: str, collection: str | None) -> str | None:
    """canonical_id 게이트(결정론) — 정규식 매칭 + doc_type prefix ↔ collection 정합.
    통과 시 cid 반환, 실패 시 None(버림 → lexical-only). collection 미지정이면 prefix 가
    매칭 정규식의 doc_type 과 같다고 보고 통과(모델이 collection 을 비웠어도 id 형식이
    규칙에 맞으면 승격 — prefix 자체가 collection 을 함의)."""
    if not cid:
        return None
    cid = cid.strip()
    for doc_type, pat in _CANONICAL_PATTERNS:
        if pat.match(cid):
            # prefix 정합: collection 이 주어졌으면 doc_type 과 일치해야 한다.
            if collection and collection != doc_type:
                return None
            return cid
    return None


class QueryFormulator:
    """N2 — Query Formulation. 프롬프트·스키마는 registry 에서 주입(SpecDrivenQuerySource)."""

    version = "spec_driven/query/v1"

    def __init__(
        self,
        llm: LLMPort,
        *,
        prompt_body: str,
        schema: dict | None = None,
        model_options: dict | None = None,
        policy_hash: str | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt_body
        self._schema = schema
        self._model_options = dict(model_options or {"temperature": 0.0})
        self._policy_hash = policy_hash

    async def formulate(
        self, query_text: str, spec: AnswerSpec, *, reasoning_label: str | None = None
    ) -> tuple[tuple[FormulatedQuery, ...], str]:
        prompt = (
            self._prompt
            .replace("{query}", query_text)
            .replace("{spec}", _render_spec(spec))
        )
        with _TRACER.start_as_current_span("intake.spec_driven_query") as span:
            oi.set_kind(span, oi.KIND_LLM)
            oi.set_io(span, input_value=prompt)
            if self._policy_hash:
                span.set_attribute("query_formulation.policy_hash", self._policy_hash)
            method = "llm"
            queries: tuple[FormulatedQuery, ...] = ()
            try:
                grammar = (
                    GrammarSpec(kind="json_schema", value=self._schema)
                    if self._schema else None
                )
                # emitter 활성 시 streaming(native CoT→thinking, 없으면 reasoning 필드
                # backstop) — N1 과 동형(설계 D2/D3). 비활성이면 현행 non-stream.
                em = current_emitter()
                lazy = LazyReasoning(reasoning_label) if em.active else None
                if lazy is not None:
                    res = await stream_capture(
                        self._llm, prompt,
                        model_options=dict(self._model_options),
                        grammar=grammar, lazy=lazy,
                    )
                else:
                    res = await self._llm.generate(
                        prompt, model_options=dict(self._model_options),
                        grammar=grammar,
                    )
                oi.set_llm(
                    span, model_name=res.model_id, prompt=prompt, completion=res.text,
                    prompt_tokens=int(res.token_usage.get("prompt_tokens", 0)),
                    completion_tokens=int(res.token_usage.get("completion_tokens", 0)),
                )
                if lazy is not None and not lazy.emitted:
                    await lazy.feed(extract_reasoning(res.text))
                queries = _parse(res.text)
            except LLMUnavailableError as exc:
                # 외부 요소(LLM 미가용)는 파싱불가(내부)와 구분해 명시 추적 — fallback
                # 쿼리로 떨어진 *이유*가 외부 미가용임을 span/로그에 남긴다(silent degrade
                # 사각지대 제거). trace_id 는 structlog _add_trace_context 가 자동 주입.
                span.set_attribute("query_formulation.upstream_error", str(exc)[:500])
                span.record_exception(exc)
                _LOG.warning("query_formulation_llm_unavailable",
                             upstream_error=str(exc)[:500],
                             error_type=type(exc).__name__,
                             model_id=getattr(self._llm, "model_id", "unknown"))
                queries = ()
            except Exception:  # noqa: BLE001 — 파싱불가 → 결정론 fallback
                queries = ()
            if not queries:
                method = "fallback"
                queries = _fallback_queries(query_text, spec)
            # 안전망 (1)·(2)·(3): refs verbatim 보장 + collection boost 유도 + 중복 제거.
            queries = _ensure_references(queries, spec.explicit_references)
            queries = _attach_targets(queries)
            queries = _dedup_queries(queries)
            span.set_attribute("query_formulation.method", method)
            span.set_attribute("query_formulation.num_queries", len(queries))
            oi.set_io(span, output_value={
                "method": method, "num_queries": len(queries),
                "queries": [q.query_text for q in queries],
            })
            return queries, method


def _render_spec(spec: AnswerSpec) -> str:
    lines = [
        f"intent: {spec.intent}",
        f"governing_normative_class: {spec.governing_normative_class or 'null'}",
        f"explicit_references: {', '.join(spec.explicit_references) or '(none)'}",
        "required_slots:",
    ]
    for s in spec.required_slots:
        kw = ", ".join(s.keywords) or "(none)"
        # facet/expected_authority 를 N2 에 노출 — 프롬프트 rule 9(facet→쿼리형태 표)가
        # 슬롯별로 쿼리·collection 을 어떻게 빚을지의 신호로 쓴다(답변 심도 §4). 미지정이면
        # 생략(net-neutral — 라벨 없는 슬롯은 기존과 동일하게 keywords 만으로 빚는다).
        tags = []
        if s.facet:
            tags.append(f"facet={s.facet}")
        if s.expected_authority:
            tags.append(f"authority={s.expected_authority}")
        tag = (" | " + " | ".join(tags)) if tags else ""
        lines.append(f"- {s.name}: keywords=[{kw}]{tag} | {s.description}".rstrip())
    return "\n".join(lines)


def _parse(text: str) -> tuple[FormulatedQuery, ...]:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return ()
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, dict):
        return ()
    raw = data.get("queries")
    if not isinstance(raw, list):
        return ()
    out: list[FormulatedQuery] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        qt = str(q.get("query_text") or "").strip()
        if not qt:
            continue
        slot = str(q.get("slot_name") or "").strip() or "query"
        target: dict[str, list[str]] = {}
        filters: dict[str, Any] = {}

        def _put(field: str, value: str, mode: str) -> None:
            # mode=filter → hard-scope(filters), 그 외(boost/누락) → 가산 boost(target).
            (filters if mode == "filter" else target)[field] = [value]

        # (1) collection — 기존 채널. 역할 구분의 1차 신호.
        coll_raw = q.get("collection")
        collection = coll_raw.strip() if isinstance(coll_raw, str) else None
        if collection in _COLLECTIONS:
            _put("collection", collection,
                 str(q.get("collection_mode") or "boost").strip().lower())
        else:
            collection = None  # enum 외/누락은 미설정으로 정규화(아래 정합 게이트 입력).

        # 무시된 채널 감사 — 모델이 *값을 냈는데* 배타성/게이트로 버려진 경우만 기록한다
        # (silent drop 금지 — 원칙 6). 모델이 애초에 안 낸 채널은 drop 이 아니므로 미기록.
        audit: dict[str, Any] = {}

        # (2) status — 규제 collection(RG/SRP/DSRS)에만 합성(§4.3 배타성 강제).
        status = q.get("status")
        if isinstance(status, str) and status in _STATUS_VALUES:
            if collection in _STATUS_COLLECTIONS:
                _put(_STATUS_FIELD, status,
                     str(q.get("status_mode") or "boost").strip().lower())
            else:
                audit["status_dropped"] = True  # 비규제 collection 에 status → 무시.

        # (3) design — nuscale_* collection 에만 합성(§5.3 배타성 강제).
        design = q.get("design")
        if isinstance(design, str) and design in _DESIGN_VALUES:
            if collection and collection.startswith(_DESIGN_COLLECTION_PREFIX):
                _put(_DESIGN_FIELD, design,
                     str(q.get("design_mode") or "boost").strip().lower())
            else:
                audit["design_dropped"] = True  # 규제/미지정 collection 에 design → 무시.

        # (4) canonical_id — 게이트(정규식 + collection prefix 정합) 통과 시만 승격(§5b.2).
        cid = q.get("canonical_id")
        if isinstance(cid, str) and cid.strip():
            valid = _validate_canonical_id(cid, collection)
            if valid:
                _put(_CANONICAL_FIELD, valid,
                     str(q.get("canonical_id_mode") or "boost").strip().lower())
            else:
                audit["canonical_id_rejected"] = True  # 정규식/prefix 불일치 → 버림.

        out.append(FormulatedQuery(slot_name=slot, query_text=qt,
                                   target=target, filters=filters,
                                   scope_audit=audit))
    return tuple(out)


def _fallback_queries(
    query_text: str, spec: AnswerSpec
) -> tuple[FormulatedQuery, ...]:
    """모델 부재/파싱불가 → 슬롯 keywords 로 슬롯당 쿼리 1개(결정론). 슬롯이 없으면
    원질의 1개."""
    out: list[FormulatedQuery] = []
    for s in spec.required_slots:
        qt = " ".join(s.keywords).strip()
        if not qt:
            continue
        out.append(FormulatedQuery(slot_name=s.name, query_text=qt))
    if not out:
        out.append(FormulatedQuery(slot_name="primary_evidence",
                                  query_text=query_text.strip()))
    return tuple(out)


def _ensure_references(
    queries: tuple[FormulatedQuery, ...], refs: tuple[str, ...]
) -> tuple[FormulatedQuery, ...]:
    """안전망 (1): 모든 explicit_reference 가 ≥1 쿼리의 query_text 에 verbatim 으로
    들어가게 보장. 모델이 슬롯 매핑을 놓쳐 누락한 ref 는 첫 쿼리에 append(lexical 앵커
    유실 방지 — advisor #4). 각 쿼리의 references 도 채운다."""
    qlist = list(queries)
    if not qlist:
        return queries
    for ref in refs:
        if not ref:
            continue
        present = any(ref.lower() in q.query_text.lower() for q in qlist)
        if not present:
            first = qlist[0]
            qlist[0] = FormulatedQuery(
                slot_name=first.slot_name,
                query_text=f"{first.query_text} {ref}".strip(),
                target=first.target,
                filters=first.filters,
                references=first.references,
                scope_audit=first.scope_audit,
            )
    # 각 쿼리에 실제 포함된 refs 기록(감사용).
    rebuilt: list[FormulatedQuery] = []
    for q in qlist:
        present_refs = tuple(r for r in refs if r and r.lower() in q.query_text.lower())
        rebuilt.append(FormulatedQuery(
            slot_name=q.slot_name, query_text=q.query_text,
            target=q.target, filters=q.filters, references=present_refs,
            scope_audit=q.scope_audit,
        ))
    return tuple(rebuilt)


def _dedup_queries(
    queries: tuple[FormulatedQuery, ...]
) -> tuple[FormulatedQuery, ...]:
    """안전망 (3): 여러 슬롯이 *동일한* query_text+scope 로 검색하는 중복을 제거한다.
    소형 모델이 슬롯 다양화에 실패해 같은 query_text 를 N개 슬롯에 그대로 복제하는 경우
    (예: section_5_4_1_{structure,content,methodology} 가 전부 "NuScale FSAR section
    5.4.1") 동일 검색을 N회 반복해 컨텍스트 예산을 같은 chunk 로 낭비하고 다양성을
    떨어뜨린다. query_text(대소문자/공백 정규화) + collection scope 가 같으면 첫 쿼리만
    남기고 접는다. 접힌 슬롯명은 살아남는 쿼리의 references 가 아니라 slot_name 으로
    이미 floor 후보가 되며, 동일 검색이라 회수 chunk·score 가 같으므로 coverage 손실은
    없다(중복 검색만 제거). 프롬프트의 '슬롯별 다양화' 지시가 1차 방어, 이 함수가 백스톱.
    """
    seen: set[tuple[str, str]] = set()
    out: list[FormulatedQuery] = []
    for q in queries:
        # scope_key 는 *모든* 채널(collection·status·design·canonical_id)을 mode 와 함께
        # 포함한다. 동일 query_text 라도 스코프가 다르면 별개 검색이므로 접지 않는다
        # (예: collection RG + status current vs history 는 다른 모집단).
        parts: list[str] = []
        for field in ("collection", _STATUS_FIELD, _DESIGN_FIELD, _CANONICAL_FIELD):
            if field in q.filters:
                parts.append(f"filter:{field}={','.join(sorted(q.filters[field]))}")
            elif field in q.target:
                parts.append(f"boost:{field}={','.join(sorted(q.target[field]))}")
        scope_key = "|".join(parts)
        key = (" ".join(q.query_text.lower().split()), scope_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return tuple(out)


def _attach_targets(
    queries: tuple[FormulatedQuery, ...]
) -> tuple[FormulatedQuery, ...]:
    """안전망 (2): query_text 의 reference 에서 collection boost 를 결정론적으로 유도해
    모델이 비운 target 을 보정(boost-only, recall-safe). 유도는 *절대 filter 로 escalate
    하지 않는다* — boost(target)만 쓴다. 모델이 collection 을 boost 든 filter 든 이미
    줬으면(둘 중 하나에 'collection' 키가 있으면) 그대로 두고 유도하지 않는다."""
    out: list[FormulatedQuery] = []
    for q in queries:
        target = dict(q.target)  # 기존 boost 채널(status/design/canonical 포함) 보존.
        has_collection = "collection" in target or "collection" in q.filters
        if not has_collection:
            coll = _derive_collection(q.query_text)
            if coll:
                target["collection"] = [coll]  # collection 만 *추가*(다른 채널 비파괴).
        out.append(FormulatedQuery(
            slot_name=q.slot_name, query_text=q.query_text,
            target=target, filters=q.filters, references=q.references,
            scope_audit=q.scope_audit,
        ))
    return tuple(out)
