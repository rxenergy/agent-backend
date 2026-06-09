from __future__ import annotations

import json

from app.domain.spec_driven import AnswerSpec, FormulatedQuery
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort

_TRACER = get_tracer("intake")

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

# 허용 collection boost 값 — corpus_map collections(nrc-all-v1 keyword 필드).
_COLLECTIONS = frozenset({"10CFR", "RG", "SRP", "DSRS", "FR"})

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
        self, query_text: str, spec: AnswerSpec
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
                res = await self._llm.generate(
                    prompt, model_options=dict(self._model_options), grammar=grammar,
                )
                oi.set_llm(
                    span, model_name=res.model_id, prompt=prompt, completion=res.text,
                    prompt_tokens=int(res.token_usage.get("prompt_tokens", 0)),
                    completion_tokens=int(res.token_usage.get("completion_tokens", 0)),
                )
                queries = _parse(res.text)
            except Exception:  # noqa: BLE001 — 미가용/파싱불가 → 결정론 fallback
                queries = ()
            if not queries:
                method = "fallback"
                queries = _fallback_queries(query_text, spec)
            # 안전망 (1)·(2): refs verbatim 보장 + collection boost 유도.
            queries = _ensure_references(queries, spec.explicit_references)
            queries = _attach_targets(queries)
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
        lines.append(f"- {s.name}: keywords=[{kw}] | {s.description}".rstrip())
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
        coll = q.get("collection")
        target: dict[str, list[str]] = {}
        if isinstance(coll, str) and coll.strip() in _COLLECTIONS:
            target = {"collection": [coll.strip()]}
        out.append(FormulatedQuery(slot_name=slot, query_text=qt, target=target))
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
                references=first.references,
            )
    # 각 쿼리에 실제 포함된 refs 기록(감사용).
    rebuilt: list[FormulatedQuery] = []
    for q in qlist:
        present_refs = tuple(r for r in refs if r and r.lower() in q.query_text.lower())
        rebuilt.append(FormulatedQuery(
            slot_name=q.slot_name, query_text=q.query_text,
            target=q.target, references=present_refs,
        ))
    return tuple(rebuilt)


def _attach_targets(
    queries: tuple[FormulatedQuery, ...]
) -> tuple[FormulatedQuery, ...]:
    """안전망 (2): query_text 의 reference 에서 collection boost 를 결정론적으로 유도해
    모델이 비운 target 을 보정(boost-only, recall-safe). 모델이 이미 collection 을 줬으면
    유지."""
    out: list[FormulatedQuery] = []
    for q in queries:
        target = dict(q.target)
        if "collection" not in target:
            coll = _derive_collection(q.query_text)
            if coll:
                target = {"collection": [coll]}
        out.append(FormulatedQuery(
            slot_name=q.slot_name, query_text=q.query_text,
            target=target, references=q.references,
        ))
    return tuple(out)
