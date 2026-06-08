from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Callable

from app.domain.finder import AnswerSpec, FinderRound
from app.domain.retrieval import RetrievedChunk
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import ChatMessage, LLMPort, ToolSpec
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

# agentic_finder N3 — Finder agentic 루프(설계 llm_tool_calling §5). 어댑터가 아니라
# application 계층이 소유한다(원칙 #1, 워크플로우가 제어). 종료는 free-text 가 아니라
# 종료용 도구 호출(submit_verdict)로 — verdict 를 구조화-by-construction 으로 만들어
# §7 계측이 항상 깨끗한 입력을 받게 한다.
#
# ⚠️ 종료 단위 = 턴이 아니라 (verdict | 재검색 라운드 | 턴 backstop). 직렬 도구 호출
# (scope→normalize→search 는 각 1턴)에서 raw 턴 카운트로 끊으면 LLM 이 검색 결과를
# follow-up 턴에서 보기 전에 루프가 끝나 verdict·재검색이 발동 못 한다. 두 카운터 분리:
#   - recover_limit: 재검색 *라운드*(retrieval.search 재발행 횟수, 첫 검색 이후).
#   - max_turns    : 총 LLM 턴 hard backstop(모델이 도구를 안 부르거나 폭주할 때).

_VERDICT_TOOL = "submit_verdict"
_SEARCH_TOOL = "retrieval.search"
_SCOPE_TOOL = "retrieval.scope"
# 용어 정규화(canonicalize)는 N1.5 conductor-invoked 로 상향됨(terminology.canonicalize,
# 보장 실행) — Finder 도구 set 에서 제거. 검색범위 확장(terminology.expand, 시소러스)은
# recover 전용으로 P3 에서 추가된다. 설계: terminology_normalization_strategy.v1.md.

# LLM-facing 도구 정의(중립 ToolSpec). registry(tools/registry.yaml)는 *실행 정책*
# (timeout/retry/span)을, 이 ToolSpec 집합은 *모델이 보는 인자 스키마*를 정한다 —
# 둘은 별개 레이어다. tools_schema_hash(재현 핀)는 이 집합의 canonical sha.
FINDER_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name=_SCOPE_TOOL,
        description="검색 범위(대상 컬렉션·필터·노이즈 floor)를 결정론적으로 산출한다. 검색 전에 호출해 범위를 좁힌다.",
        parameters={
            "type": "object",
            "properties": {
                "entities": {"type": "object", "description": "정규화된 엔티티(노형명·규제 ID 등)"},
                "intents": {"type": "array", "items": {"type": "string"}},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name=_SEARCH_TOOL,
        description="정규화된 질의와 범위 파라미터로 하이브리드 검색을 수행한다(Reranker 정렬). 불충분하면 슬롯을 겨냥해 재호출.",
        parameters={
            "type": "object",
            "properties": {
                "query_text": {"type": "string", "description": "검색 질의(정규화 반영)"},
                "top_k": {"type": "integer"},
                "target": {"type": "object", "description": "retrieval.scope 의 target(boost)"},
                "filters": {"type": "object", "description": "retrieval.scope 의 filters(hard)"},
                "min_token_count": {"type": "integer"},
            },
            "required": ["query_text"],
        },
    ),
    ToolSpec(
        name=_VERDICT_TOOL,
        description="검색 결과가 답변 사양의 슬롯을 충족하는지 판정해 루프를 종료한다.",
        parameters={
            "type": "object",
            "properties": {
                "sufficient": {"type": "boolean"},
                "missing_slots": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
            "required": ["sufficient", "reason"],
        },
    ),
)


def tools_schema_hash() -> str:
    """FINDER_TOOL_SPECS 집합의 canonical sha16(재현 핀 — tools_schema_hash)."""
    canon = json.dumps(
        [{"name": t.name, "description": t.description, "parameters": t.parameters}
         for t in FINDER_TOOL_SPECS],
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


@dataclass
class FinderResult:
    chunks: list[RetrievedChunk]
    finder_rounds: list[FinderRound]
    verdict: dict[str, Any]
    recover_limit_hit: bool
    turns_used: int
    llm_calls: int
    finder_policy_hash: str | None
    tools_schema_hash: str


def render_answer_spec(spec: AnswerSpec) -> str:
    """답변 사양을 Finder 시스템 프롬프트에 덧붙일 텍스트로 렌더(llm_tool_calling §5
    'system(finder 지시 + 답변 사양/슬롯)')."""
    lines = ["", "## 답변 사양(찾아야 할 정보 슬롯)"]
    if spec.required_slots:
        for s in spec.required_slots:
            tag = "필수" if s.required else "보강"
            desc = f" — {s.description}" if s.description else ""
            lines.append(f"- {s.name} ({tag}){desc}")
    else:
        lines.append("- (슬롯 미지정 — 질의에 필요한 근거를 폭넓게 찾는다)")
    if spec.answer_structure:
        lines.append(f"\n답변 구조: {spec.answer_structure}")
    return "\n".join(lines)


async def run_finder(
    *,
    llm: LLMPort,
    tool_executor: Any,
    ctx: ToolExecutionContext,
    system_prompt_body: str,
    finder_policy_hash: str | None,
    query_text: str,
    answer_spec: AnswerSpec,
    record: Callable[[Any], None],
    recover_limit: int = 3,
    max_turns: int = 10,
    model_options: dict[str, Any] | None = None,
    terminology_annotation: str | None = None,
) -> FinderResult:
    """Finder tool-calling 멀티턴 루프. 종료 = (verdict | research_rounds≥recover_limit
    | max_turns backstop). chunks 를 누적해 다운스트림(N4/Generation)으로 넘긴다.

    `terminology_annotation`(N1.5 canonicalize 산출 병기) — 정규형·정의를 system 에
    덧붙여 Finder 가 정밀 질의를 짜게 한다. render_answer_spec 과 동일한 *런타임 컨텐츠*
    라 finder_policy_hash(정적 prompt_body sha)는 불변, rendered prompt 만 바뀐다."""
    system_text = system_prompt_body + "\n" + render_answer_spec(answer_spec)
    if terminology_annotation:
        system_text = system_text + "\n" + terminology_annotation
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_text),
        ChatMessage(role="user", content=query_text),
    ]

    verdict: dict[str, Any] | None = None
    research_rounds = 0      # retrieval.search 재발행 횟수(첫 검색 이후).
    searched_once = False
    chunks_by_id: dict[str, RetrievedChunk] = {}
    finder_rounds: list[FinderRound] = []
    # per-round 누적기(직렬 도구 호출 — scope 는 search 전 별도 턴).
    pending_scope: dict[str, Any] = {}
    llm_calls = 0
    turns_used = 0

    with _TRACER.start_as_current_span("agent.finder") as agent_span:
        oi.set_kind(agent_span, oi.KIND_AGENT)
        agent_span.set_attribute("finder.recover_limit", recover_limit)
        agent_span.set_attribute("finder.max_turns", max_turns)
        if finder_policy_hash:
            agent_span.set_attribute("finder.policy_hash", finder_policy_hash)
        agent_span.set_attribute("finder.tools_schema_hash", tools_schema_hash())

        for _turn in range(max_turns):
            turns_used += 1
            with _TRACER.start_as_current_span("llm.finder_turn") as s:
                oi.set_kind(s, oi.KIND_LLM)
                r = await llm.generate_with_tools(
                    messages, tools=list(FINDER_TOOL_SPECS),
                    tool_choice="required",  # 매 턴 도구 호출 강제.
                    model_options=model_options,
                )
                llm_calls += 1
                # §7 계측 — 자식 span 에 tool_choice/stop_reason/tool_call 수.
                s.set_attribute("llm.tool_choice", "required")
                s.set_attribute("llm.stop_reason", r.stop_reason)
                s.set_attribute("llm.num_tool_calls", len(r.tool_calls))

            messages.append(ChatMessage(role="assistant", content=r.text,
                                        tool_calls=r.tool_calls))
            if not r.tool_calls:
                # 모델이 도구를 안 불렀다(tools 미지원 등, §9) — 재검색 미발생 →
                # recover_limit 안 걸리므로 max_turns 가 backstop. 다음 턴으로.
                continue

            broke_on_verdict = False
            for call in r.tool_calls:
                out = await tool_executor.invoke(call.name, call.arguments, ctx)
                record(out)
                ok = out.status != "failed"
                if call.name == _VERDICT_TOOL:
                    verdict = dict(out.output or call.arguments or {})
                    messages.append(ChatMessage(
                        role="tool", content=_serialize(out.output),
                        tool_call_id=call.id, is_error=not ok))
                    broke_on_verdict = True
                    break
                if call.name == _SCOPE_TOOL and ok:
                    pending_scope = dict(out.output or {})
                elif call.name == _SEARCH_TOOL:
                    if searched_once:
                        research_rounds += 1
                    searched_once = True
                    new_chunks, rerank_scores = _parse_search(out.output if ok else None)
                    for c in new_chunks:
                        chunks_by_id[c.chunk_id] = c
                    # per-search FinderRound 확정(advisor: 1 라운드 = 1 검색).
                    # normalized_terms 는 () — 정규화는 N1.5 conductor(라운드 단위 아님,
                    # query_understanding.terminology 가 핀). P3 에서 recover 확장
                    # expanded_terms 필드 추가 예정.
                    finder_rounds.append(FinderRound(
                        round_index=len(finder_rounds),
                        query=str((call.arguments or {}).get("query_text") or query_text),
                        scope_params=pending_scope,
                        num_chunks=len(new_chunks),
                        reranker_score_dist=tuple(rerank_scores),
                    ))
                    pending_scope = {}
                messages.append(ChatMessage(
                    role="tool", content=_serialize(out.output),
                    tool_call_id=call.id, is_error=not ok))

            if broke_on_verdict:
                break
            if research_rounds >= recover_limit:
                break  # 재검색 라운드 소진 → 강제 종료.

        recover_limit_hit = verdict is None and research_rounds >= recover_limit
        if verdict is None:
            # verdict 미산출 종료(recover_limit/max_turns) → synthetic sufficient=False.
            verdict = {
                "sufficient": False,
                "missing_slots": [s.name for s in answer_spec.required_slots],
                "reason": ("recover_limit reached" if recover_limit_hit
                           else "max_turns backstop reached without verdict"),
            }
        # verdict 를 직전(마지막) 검색 라운드에 귀속 — LLM 이 그 결과를 보고 판정했다.
        if finder_rounds:
            finder_rounds[-1] = replace(
                finder_rounds[-1],
                verdict_sufficient=bool(verdict.get("sufficient", False)),
                missing_slots=tuple(verdict.get("missing_slots") or ()),
                verdict_reason=str(verdict.get("reason") or ""),
            )

        agent_span.set_attribute("finder.research_rounds", research_rounds)
        agent_span.set_attribute("finder.recover_limit_hit", recover_limit_hit)
        agent_span.set_attribute("finder.num_chunks", len(chunks_by_id))
        agent_span.set_attribute("finder.verdict_sufficient",
                                 bool(verdict.get("sufficient", False)))
        oi.set_io(agent_span, input_value=query_text, output_value={
            "research_rounds": research_rounds, "num_chunks": len(chunks_by_id),
            "verdict_sufficient": verdict.get("sufficient"),
        })

    return FinderResult(
        chunks=list(chunks_by_id.values()),
        finder_rounds=finder_rounds,
        verdict=verdict,
        recover_limit_hit=recover_limit_hit,
        turns_used=turns_used,
        llm_calls=llm_calls,
        finder_policy_hash=finder_policy_hash,
        tools_schema_hash=tools_schema_hash(),
    )


def _serialize(output: Any) -> str:
    try:
        return json.dumps(output or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(output)


def _parse_search(output: Any) -> tuple[list[RetrievedChunk], list[float]]:
    if not isinstance(output, dict):
        return [], []
    chunks: list[RetrievedChunk] = []
    for raw in output.get("chunks", []) or []:
        try:
            chunks.append(RetrievedChunk.model_validate(raw))
        except Exception:  # noqa: BLE001 — 깨진 chunk 는 건너뛴다(부분 진행).
            continue
    scores = [float(x) for x in (output.get("rerank_scores") or [])]
    return chunks, scores
