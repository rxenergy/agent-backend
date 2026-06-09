from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.domain.retrieval import RetrievedChunk
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import ChatMessage, LLMPort, ToolSpec
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

# react_minimal_v1 N1 — ReAct(Thought→Action→Observation) Retrieval 루프
# (docs/plans/react_minimal_workflow.v1). finder_loop 와 동형(application 계층이 소유,
# 원칙 #1)이되 더 단순하다: 분류기·answer_spec·recover_limit·multi-hop 없이 모델
# 추론이 도구 사용을 주도한다(Yao et al. 2022 ReAct).
#
# ⚠️ finder_loop 를 in-place 변형하지 않는다 — finder_loop 는 agentic_finder_v4 가
# import 하고 test_finder_loop.py 가 도구 set·schema hash 를 핀한다. 별도 모듈로 패턴만
# 재사용한다.
#
# 종료 단위 = (submit_response | max_turns backstop). finder 의 recover_limit(재검색
# 라운드 상한)은 없다 — 모델이 언제 충분한지 스스로 판정(submit_response)하고, max_turns
# 가 무한 루프 backstop 이다.

_FINISH_TOOL = "submit_response"
_SEARCH_TOOL = "retrieval.search"
_SCOPE_TOOL = "confidence.scope"
_CORPUS_SCOPE_TOOL = "retrieval.scope"

# submit_response 종료 신호 어휘 — 루프가 ReAct 종료 프로토콜의 주인이므로 여기서
# 정의하고, adapter(submit_response)가 import 한다(adapters→application 방향 준수).
# answer/out_of_scope/clarification/insufficient_evidence. 미상/누락은 fail-safe 로
# insufficient_evidence(근거 없이 answer 로 떨어지지 않게).
VALID_OUTCOMES = frozenset(
    {"answer", "out_of_scope", "clarification", "insufficient_evidence"}
)
_FALLBACK_OUTCOME = "insufficient_evidence"

# submit_response ToolSpec — 두 도구 세트(REACT_TOOL_SPECS / REACT_ECHO_TOOL_SPECS)가
# 동일 종료 계약을 공유하도록 단일 출처로 둔다(enum=VALID_OUTCOMES 단일 정의). conductor
# 의 outcome→RefusalReason 매핑은 세트와 무관하게 동일하다.
_SUBMIT_RESPONSE_SPEC = ToolSpec(
    name="submit_response",
    description=(
        "Finish the retrieval phase with your judgment. outcome='answer' when the "
        "evidence covers the question; 'out_of_scope' when the query is off-domain / "
        "asks you to fabricate or to give legal-licensing authority; 'clarification' "
        "when you must ask which reactor / regulation / RAI; 'insufficient_evidence' "
        "when you searched but key facts are missing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": sorted(VALID_OUTCOMES),
            },
            "reason": {"type": "string"},
            "missing_info": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["outcome", "reason"],
    },
)

# LLM-facing 도구 정의(중립 ToolSpec). registry(tools/registry.yaml)는 *실행 정책*
# (timeout/retry/span)을, 이 ToolSpec 집합은 *모델이 보는 인자 스키마*를 정한다.
# tools_schema_hash(재현 핀)는 이 집합의 canonical sha. 설명은 영어(소형 모델 대상
# ReAct 프롬프트와 언어 일치).
REACT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="confidence.scope",
        description=(
            "Self-assess whether the query is within the SMR licensing / nuclear "
            "regulation domain and how well its terms are understood. Returns a "
            "coverage signal and the list of unknown (unresolved) terms you should "
            "resolve before searching. Call this FIRST."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query_text": {"type": "string"},
                "terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key terms / reactor names / regulation ids in the query.",
                },
                "entities": {
                    "type": "object",
                    "description": "Optional grouped entities (e.g. reactor, regulation).",
                },
            },
            "required": ["query_text"],
        },
    ),
    ToolSpec(
        name="terminology.canonicalize",
        description=(
            "Resolve surface terms to their canonical form + definition using the "
            "controlled vocabulary (e.g. 'emergency core cooling' -> ECCS). Use it on "
            "unknown/unresolved terms to fill definition gaps before searching."
        ),
        parameters={
            "type": "object",
            "properties": {
                "terms": {"type": "array", "items": {"type": "string"}},
                "query_en": {"type": "string"},
            },
            "required": ["terms"],
        },
    ),
    ToolSpec(
        name="terminology.expand",
        description=(
            "Broaden a term to synonyms (uf) / narrower terms (nt) when a precise "
            "search came back thin. Use AFTER a first retrieval.search, then re-search "
            "with the expanded terms. Related terms (rt) drift off-topic — use sparingly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "terms": {"type": "array", "items": {"type": "string"}},
                "relations": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["uf", "nt", "rt"]},
                },
                "max_per_term": {"type": "integer"},
            },
            "required": ["terms"],
        },
    ),
    ToolSpec(
        name="retrieval.scope",
        description=(
            "Compute deterministic search scope (target collections / filters / noise "
            "floor) from entities. Optional — call before retrieval.search to narrow it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entities": {"type": "object"},
                "intents": {"type": "array", "items": {"type": "string"}},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="retrieval.search",
        description=(
            "Hybrid search over the indexed corpus (reranked). query_text is required; "
            "pass retrieval.scope's target/filters/min_token_count if available. Inspect "
            "the returned chunks yourself; re-search to fill gaps."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query_text": {"type": "string"},
                "top_k": {"type": "integer"},
                "target": {"type": "object"},
                "filters": {"type": "object"},
                "min_token_count": {"type": "integer"},
            },
            "required": ["query_text"],
        },
    ),
    _SUBMIT_RESPONSE_SPEC,
)

# react_echo_v1 N1 — 도구-최소 ReAct 세트(retrieval.search + submit_response 만).
# confidence.scope·terminology.canonicalize·terminology.expand·retrieval.scope 를 제거해
# *검색 질의 작성을 전적으로 모델 추론에 맡긴다*(docs/plans 의 echo variant). 핵심은
# 질의 원문의 도메인 키워드(노형명·규제 ID·RAI 번호·기술 약어)를 보존한 query_text 를
# 모델이 직접 만드는 것 — 정규화/확장 도구가 키워드를 치환·소실시키지 않는다. retrieval
# .search 스펙도 target/filters 를 빼 query_text 단일 입력으로 좁힌다(retrieval.scope
# 부재). submit_response 는 동일 종료 계약(_SUBMIT_RESPONSE_SPEC)을 공유한다.
REACT_ECHO_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="retrieval.search",
        description=(
            "Hybrid search over the indexed corpus (reranked). Build query_text by "
            "PRESERVING the domain keywords of the user's question verbatim — reactor "
            "names (NuScale, i-SMR), regulation ids (10 CFR 50.46, RG 1.157, GDC 35), "
            "RAI numbers, technical acronyms (ECCS, LOCA, DNBR). Add canonical / "
            "synonym forms alongside them; never drop or paraphrase a keyword away. "
            "Inspect the returned chunks yourself; re-search to fill gaps, still "
            "keeping the original keywords."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query_text": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query_text"],
        },
    ),
    _SUBMIT_RESPONSE_SPEC,
)


def tools_schema_hash(specs: tuple[ToolSpec, ...] = REACT_TOOL_SPECS) -> str:
    """ToolSpec 집합의 canonical sha16(재현 핀). finder 판과 달리 set 을 인자로 받아
    핀이 *정확히 이 set* 을 반영한다."""
    canon = json.dumps(
        [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in specs
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


@dataclass
class ReactResult:
    chunks: list[RetrievedChunk]
    outcome: str
    reason: str
    missing_info: tuple[str, ...]
    turns_used: int
    llm_calls: int
    retrieval_policy_hash: str | None
    tools_schema_hash: str
    term_coverage: float | None = None
    corpus_map_hash: str | None = None
    scope_mode: str | None = None
    tool_result_refs: list[str] = field(default_factory=list)


async def run_react(
    *,
    llm: LLMPort,
    tool_executor: Any,
    ctx: ToolExecutionContext,
    system_prompt_body: str,
    retrieval_policy_hash: str | None,
    query_text: str,
    record: Callable[[Any], None],
    max_turns: int = 8,
    model_options: dict[str, Any] | None = None,
    tool_specs: tuple[ToolSpec, ...] = REACT_TOOL_SPECS,
) -> ReactResult:
    """ReAct Retrieval 루프. 종료 = (submit_response | max_turns backstop). chunks 를
    누적해 Generation 으로 넘긴다. 모델 추론(Thought)은 assistant.content 에 실려
    도구 호출과 함께 흐른다 — tool_choice='required' 라도 추론은 보존된다(D2)."""
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_prompt_body),
        ChatMessage(role="user", content=query_text),
    ]

    chunks_by_id: dict[str, RetrievedChunk] = {}
    tool_result_refs: list[str] = []
    outcome: str | None = None
    reason = ""
    missing_info: tuple[str, ...] = ()
    term_coverage: float | None = None
    corpus_map_hash: str | None = None
    scope_mode: str | None = None
    llm_calls = 0
    turns_used = 0

    with _TRACER.start_as_current_span("agent.react_retrieval") as agent_span:
        oi.set_kind(agent_span, oi.KIND_AGENT)
        agent_span.set_attribute("react.max_turns", max_turns)
        agent_span.set_attribute("react.tools_schema_hash", tools_schema_hash(tool_specs))
        if retrieval_policy_hash:
            agent_span.set_attribute("react.policy_hash", retrieval_policy_hash)

        for _turn in range(max_turns):
            turns_used += 1
            with _TRACER.start_as_current_span("llm.react_turn") as s:
                oi.set_kind(s, oi.KIND_LLM)
                r = await llm.generate_with_tools(
                    messages,
                    tools=list(tool_specs),
                    tool_choice="required",  # D2: 매 턴 도구 호출 강제(추론은 content 보존).
                    model_options=model_options,
                )
                llm_calls += 1
                s.set_attribute("llm.tool_choice", "required")
                s.set_attribute("llm.stop_reason", r.stop_reason)
                s.set_attribute("llm.num_tool_calls", len(r.tool_calls))

            messages.append(
                ChatMessage(role="assistant", content=r.text, tool_calls=r.tool_calls)
            )
            if not r.tool_calls:
                # 모델이 도구를 안 불렀다(tools 미지원 등) — max_turns 가 backstop.
                continue

            broke_on_finish = False
            for call in r.tool_calls:
                out = await tool_executor.invoke(call.name, call.arguments, ctx)
                record(out)
                ok = out.status != "failed"
                if out.output_hash:
                    tool_result_refs.append(out.output_hash)

                if call.name == _FINISH_TOOL:
                    fin = dict(out.output or call.arguments or {})
                    outcome = str(fin.get("outcome") or _FALLBACK_OUTCOME)
                    reason = str(fin.get("reason") or "")
                    missing_info = tuple(fin.get("missing_info") or ())
                    messages.append(
                        ChatMessage(role="tool", content=_serialize(out.output),
                                    tool_call_id=call.id, is_error=not ok)
                    )
                    broke_on_finish = True
                    break

                if call.name == _SEARCH_TOOL:
                    new_chunks = _parse_chunks(out.output if ok else None)
                    for c in new_chunks:
                        chunks_by_id[c.chunk_id] = c
                elif call.name == _SCOPE_TOOL and ok:
                    so = out.output or {}
                    tc = so.get("term_coverage")
                    if isinstance(tc, (int, float)):
                        term_coverage = float(tc)
                    corpus_map_hash = so.get("corpus_map_hash") or corpus_map_hash
                elif call.name == _CORPUS_SCOPE_TOOL and ok:
                    # retrieval.scope(결정론 코퍼스 범위) — scope_mode 재현 핀 포착.
                    so = out.output or {}
                    scope_mode = so.get("mode") or scope_mode
                    corpus_map_hash = so.get("corpus_map_hash") or corpus_map_hash

                messages.append(
                    ChatMessage(role="tool", content=_serialize(out.output),
                                tool_call_id=call.id, is_error=not ok)
                )

            if broke_on_finish:
                break

        if outcome is None:
            # submit_response 없이 종료(max_turns) → 근거부족으로 단락(절대 answer 아님).
            outcome = "insufficient_evidence"
            reason = "max_turns backstop reached without submit_response"

        agent_span.set_attribute("react.turns_used", turns_used)
        agent_span.set_attribute("react.finish_outcome", outcome)
        agent_span.set_attribute("react.num_chunks", len(chunks_by_id))
        oi.set_io(agent_span, input_value=query_text, output_value={
            "outcome": outcome, "num_chunks": len(chunks_by_id),
            "turns_used": turns_used,
        })

    return ReactResult(
        chunks=list(chunks_by_id.values()),
        outcome=outcome,
        reason=reason,
        missing_info=missing_info,
        turns_used=turns_used,
        llm_calls=llm_calls,
        retrieval_policy_hash=retrieval_policy_hash,
        tools_schema_hash=tools_schema_hash(tool_specs),
        term_coverage=term_coverage,
        corpus_map_hash=corpus_map_hash,
        scope_mode=scope_mode,
        tool_result_refs=tool_result_refs,
    )


def _serialize(output: Any) -> str:
    try:
        return json.dumps(output or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(output)


def _parse_chunks(output: Any) -> list[RetrievedChunk]:
    if not isinstance(output, dict):
        return []
    chunks: list[RetrievedChunk] = []
    for raw in output.get("chunks", []) or []:
        try:
            chunks.append(RetrievedChunk.model_validate(raw))
        except Exception:  # noqa: BLE001 — 깨진 chunk 는 건너뛴다(부분 진행).
            continue
    return chunks
