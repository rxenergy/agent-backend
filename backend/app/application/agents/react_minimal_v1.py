from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from typing import Any, AsyncIterator

from app.application.agents.events import (
    AgentEvent,
    EventEmitter,
    bind_emitter,
    current_emitter,
    emit_reasoning,
    emit_step,
    emit_token,
    emit_tool_nowait,
    unbind_emitter,
)
from app.application.agents.react_loop import REACT_TOOL_SPECS, ReactResult, run_react
from app.application.agents.registry import AgentDeps, register_variant
from app.application.agents.llm_router import LLMRouter, UnknownLLMError
from app.application.context.pack import ContextBuilder, ContextPack
from app.application.events.recorder import EventRecorder
from app.domain.agents import VariantSpec
from app.domain.errors import RefusalReason, VerificationStatus
from app.domain.interaction import AgentRequest, AgentResponse, Citation, ToolCallRecord
from app.observability import openinference as oi
from app.observability.metrics import get_metrics
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMResult, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

REACT_MINIMAL_VARIANT_ID = "react_minimal_v1"

# answer 본문에서 실제 사용한 인용 마커 추출(검증 입력 referenced_citation_ids).
_CITE_RE = re.compile(r"\[(cite-\d+)\]")


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ReactMinimalRunner:
    """react_minimal_v1 — 최소 2-Phase **Retrieval(ReAct 루프) → Generation** variant
    (docs/plans/react_minimal_workflow.v1).

    기존 heavy variant(agentic_finder_v4, hierarchical_corrective_v3_1)의 다수 필수
    노드(query_translate·분류·terminology canonicalize·answer_spec·memory gating·
    multi-hop)를 *제거*하고, 모델 추론이 워크플로우를 주도하는 ReAct(Thought→Action→
    Observation, Yao et al. 2022) 베이스라인이다. 플랫폼 불변식(재현성·통제된 도구·
    거부 1급·단일 이미지·SDK-free)은 유지한다.

    Phase 1 Retrieval — run_react: confidence.scope→terminology.*→retrieval.*→
      submit_response 의 tool-calling 루프. 라우팅은 *모델 주도*(분류기 없음): 모델이
      submit_response.outcome 으로 scope/명료화/근거부족을 표현하고, conductor 가
      outcome→RefusalReason 으로 결정한다(표현=모델 / 결정=코드).
    Phase 2 Generation — 단일 ReAct 생성 프롬프트(O×D resolver 미사용) + ContextBuilder
      + 인용 계약. 생성 후 검증은 *관측 전용*(D1, [[generation-verification-coupling]]):
      스트리밍 텍스트는 되돌릴 수 없으므로 사전 게이트가 아니라 audit 다."""

    # Phase 1 ReAct 루프에 노출하는 도구 세트(독립 변수). react_echo_v1 은 이 한 줄만
    # REACT_ECHO_TOOL_SPECS 로 override 해 도구-최소 변형이 된다 — 루프 mechanics·생성·
    # 검증·이벤트 발행 harness 는 공유한다(실험 변수 격리). tools_schema_hash 는 이 세트
    # 에서 파생되므로 두 variant 의 InteractionEvent 는 자연히 구별된다(원칙 5).
    _tool_specs = REACT_TOOL_SPECS

    def __init__(
        self,
        *,
        spec: VariantSpec,
        llm_router: LLMRouter,
        tool_executor: Any,
        context_builder: ContextBuilder,
        recorder: EventRecorder,
        event_sink: EventSinkPort,
        app_profile: str,
        utility_llm: LLMPort | None = None,
        # N1 ReAct 시스템 프롬프트 source(registry 호스팅, sha 핀). None 이면 부트 배선
        # 오류(프롬프트 인라인 금지 — finder_source 와 동일 fail-fast).
        react_retrieval_source: Any = None,
        # N2 생성 프롬프트 source(registry 호스팅, sha 핀). None 이면 부트 배선 오류.
        react_generation_source: Any = None,
        citation_contract_path: str | None = None,
        react_max_turns: int = 8,
        verification_citation_threshold: float = 0.9,
        verification_faithfulness_threshold: float = 0.85,
    ) -> None:
        self.spec = spec
        self._llm_router = llm_router
        self._utility_llm = utility_llm
        self._tools = tool_executor
        # snippets 모드 — chunk window 가 생성 프롬프트 evidence 로 닿게(v4 와 동형).
        self._context_builder = ContextBuilder(capture_mode="snippets")
        self._recorder = recorder
        self._sink = event_sink
        self._app_profile = app_profile
        self._react_retrieval_source = react_retrieval_source
        self._react_generation_source = react_generation_source
        self._react_max_turns = react_max_turns
        self._cite_threshold = verification_citation_threshold
        self._faith_threshold = verification_faithfulness_threshold
        # 인용 계약 preamble — 한 번 로드, context block 앞에 붙여 rendered_prompt_hash
        # 에 반영(v4 와 동일 idiom). 생성 프롬프트가 인용 규칙을 이미 담지만 계약을
        # 별도 prepend 해 두 variant 가 동일 계약을 공유하게 한다.
        self._citation_contract: str | None = None
        self._citation_contract_sha: str | None = None
        if citation_contract_path:
            from pathlib import Path

            p = Path(citation_contract_path)
            if p.is_file():
                self._citation_contract = p.read_text(encoding="utf-8")
                self._citation_contract_sha = _sha16(self._citation_contract)

    # ------------------------------------------------------------------
    # Streaming wrapper — v2/v3.1/v4 와 동일 패턴(검증됨).
    # ------------------------------------------------------------------
    async def run_stream(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        emitter = EventEmitter(active=True)
        token = bind_emitter(emitter)
        response: AgentResponse | None = None
        run_error: BaseException | None = None

        async def _drive() -> None:
            nonlocal response
            try:
                response = await self.run(request)
            finally:
                await emitter.close()

        task = asyncio.create_task(_drive())
        try:
            async for ev in emitter.drain():
                yield ev
            try:
                await task
            except BaseException as exc:  # noqa: BLE001
                run_error = exc
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            unbind_emitter(token)

        if run_error is not None:
            yield AgentEvent(
                kind="error",
                payload={"message": str(run_error), "type": type(run_error).__name__},
                ts=time.monotonic(),
            )
            return
        if response is not None:
            yield AgentEvent(kind="final", payload={"response": response},
                             ts=time.monotonic())

    # ------------------------------------------------------------------
    # 2-Phase conductor.
    # ------------------------------------------------------------------
    async def run(self, request: AgentRequest) -> AgentResponse:
        started = time.monotonic()
        metrics = get_metrics()
        tool_calls: list[ToolCallRecord] = []
        tool_result_refs: list[str] = []

        def record(r) -> None:
            tool_calls.append(
                ToolCallRecord(
                    name=r.tool_name, version=r.tool_version, status=r.status,
                    latency_ms=r.latency_ms, input_hash=r.input_hash,
                    output_hash=r.output_hash, error_code=r.error_code,
                    retry_count=r.retry_count,
                )
            )
            if r.output_hash:
                tool_result_refs.append(r.output_hash)
            metrics.record_tool(tool=r.tool_name, status=r.status,
                                retry_count=r.retry_count)
            emit_tool_nowait(
                r.tool_name, r.status, version=r.tool_version,
                latency_ms=r.latency_ms, error_code=r.error_code,
                retry_count=r.retry_count,
            )

        try:
            llm_id, llm = self._llm_router.resolve(request.model or None)
        except UnknownLLMError:
            llm_id, llm = self._llm_router.resolve(None)

        with _TRACER.start_as_current_span("agent.run") as root:
            root.set_attribute("interaction_id", request.interaction_id)
            root.set_attribute("agent.variant", self.spec.variant_id)
            root.set_attribute("llm_id", llm_id)
            oi.set_kind(root, oi.KIND_AGENT)
            oi.set_io(root, input_value=request.query_text)

            # 분류기 없음 → scenario_object/depth 미설정. CorpusMap.resolve_scope 는
            # None 을 허용하고(scenario_object: str|None) entities/intents 로 매칭한다.
            ctx = ToolExecutionContext(
                interaction_id=request.interaction_id, trace_id="",
                app_profile=self._app_profile, agent_variant=self.spec.variant_id,
                session_id=request.session_id, user_id=request.user_id,
                project_id=request.project_id,
            )

            # === Phase 1 Retrieval (ReAct 루프) ============================
            await emit_step("react_retrieval", "started")
            if self._react_retrieval_source is None:
                raise RuntimeError(
                    "react_retrieval_source not wired — N1 system prompt is "
                    "registry-hosted (prompts/registry.yaml react_retrieval_prompts)"
                )
            react = await run_react(
                llm=llm,
                tool_executor=self._tools,
                ctx=ctx,
                system_prompt_body=self._react_retrieval_source.prompt_body,
                retrieval_policy_hash=self._react_retrieval_source.policy_hash,
                query_text=request.query_text,
                record=record,
                max_turns=self._react_max_turns,
                model_options=self._react_retrieval_source.model_options or None,
                tool_specs=self._tool_specs,
            )
            for ref in react.tool_result_refs:
                if ref not in tool_result_refs:
                    tool_result_refs.append(ref)
            await emit_step("react_retrieval", "ok",
                            outcome=react.outcome, num_chunks=len(react.chunks),
                            turns=react.turns_used)

            # 재현 핀(원칙 5) — 루프 산출을 query_understanding 백에 싣는다(v4 idiom;
            # InteractionEvent 전용 필드 없이 기존 스키마로 재현성 충족).
            qu_pin: dict[str, Any] = {
                "react_retrieval": {
                    "policy_hash": react.retrieval_policy_hash,
                    "tools_schema_hash": react.tools_schema_hash,
                    "turns_used": react.turns_used,
                    "finish_outcome": react.outcome,
                    "term_coverage": react.term_coverage,
                }
            }

            # === Routing gate (결정론, 모델 outcome → action) ==============
            refusal = _OUTCOME_REFUSAL.get(react.outcome)
            if refusal is not None:
                return await self._refuse(
                    request, started, tool_calls, refusal,
                    error_code=refusal.value, query_understanding=qu_pin,
                    corpus_map_hash=react.corpus_map_hash, scope_mode=react.scope_mode,
                )
            if not react.chunks:
                # outcome=answer 인데 근거 0 → 강제 거부(모델이 근거 없이 답하게 둘 수
                # 없다 — "결정=코드"). RETRIEVAL_NO_RESULT 로 단락.
                return await self._refuse(
                    request, started, tool_calls, RefusalReason.RETRIEVAL_NO_RESULT,
                    error_code="retrieval_no_result", query_understanding=qu_pin,
                    corpus_map_hash=react.corpus_map_hash, scope_mode=react.scope_mode,
                )

            # === Phase 2 Generation =======================================
            await emit_step("context_build", "started")
            with _TRACER.start_as_current_span("agent.context_build") as s:
                pack = self._context_builder.build(
                    interaction_id=request.interaction_id,
                    query_text=request.query_text,
                    chat_history=(),  # stateless — no memory.
                    conversation_summary=None,
                    scenario_object="n_a", scenario_depth="n_a",
                    entities={}, chunks=react.chunks, memory_refs=(),
                    tool_result_refs=tuple(tool_result_refs),
                )
                s.set_attribute("context_hash", pack.context_hash)
                oi.set_kind(s, oi.KIND_RETRIEVER)
                oi.set_io(s, input_value=request.query_text, output_value={
                    "context_hash": pack.context_hash, "num_chunks": len(react.chunks),
                })
            await emit_step("context_build", "ok", context_hash=pack.context_hash)

            await emit_step("prompt_render", "started")
            rendered_text = self._render_generation_prompt(request.query_text, pack)
            rendered_prompt_hash = _sha16(rendered_text)
            await self._sink.write_context_snapshot(
                request.interaction_id, self._context_builder.to_snapshot(pack),
            )
            await emit_step("prompt_render", "ok",
                            profile_id="react_generation_v1",
                            rendered_prompt_hash=rendered_prompt_hash)

            await emit_step("generation", "started", llm_id=llm_id)
            llm_result = await self._generate(
                request, rendered_text, started, tool_calls, llm=llm,
                query_understanding=qu_pin,
            )
            if isinstance(llm_result, AgentResponse):
                return llm_result  # LLM-unavailable refusal
            await emit_step("generation", "ok",
                            completion_tokens=llm_result.token_usage.get("completion_tokens", 0))
            metrics.record_tokens(
                prompt_tokens=int(llm_result.token_usage.get("prompt_tokens", 0)),
                completion_tokens=int(llm_result.token_usage.get("completion_tokens", 0)),
            )

            citations = _to_citations(pack.citation_candidates)
            chunk_ids = [c.chunk_id for c in react.chunks]

            # === 생성 후 검증 — 관측 전용(D1) ==============================
            verification_status, cite_completeness, faithfulness = (
                await self._verify(llm_result.text, citations, chunk_ids, ctx, record)
            )

            response = AgentResponse(
                interaction_id=request.interaction_id,
                answer_text=llm_result.text,
                citations=citations,
                refusal_reason=None,
                verification_status=verification_status,
                scenario_object="n_a",
                scenario_depth="n_a",
                latency_ms=int((time.monotonic() - started) * 1000),
                token_usage=dict(llm_result.token_usage),
                llm_id=llm_id,
                model_id=llm_result.model_id,
                regulatory_grounding="n_a",
            )
            metrics.record_terminal(outcome="answer", latency_ms=response.latency_ms,
                                    scenario_object="n_a", scenario_depth="n_a")

            with _TRACER.start_as_current_span("event.persist") as s:
                event = self._recorder.build(
                    request=request, response=response,
                    agent_variant=self.spec.variant_id,
                    retrieved_chunk_ids=tuple(chunk_ids),
                    retrieval_confidence=(getattr(react.chunks[0], "score", 0.0)
                                          if react.chunks else 0.0),
                    prompt_profile_id="react_generation_v1",
                    prompt_version=self._react_generation_source.prompt_version,
                    rendered_prompt_hash=rendered_prompt_hash,
                    prompt_composition_hash=self._react_generation_source.policy_hash,
                    prompt_source="local",
                    context_hash=pack.context_hash,
                    citation_completeness=cite_completeness,
                    faithfulness=faithfulness,
                    started_at=started,
                    tool_calls=tuple(tool_calls),
                    corpus_map_hash=react.corpus_map_hash,
                    scope_mode=react.scope_mode,
                    regulatory_grounding="n_a",
                    query_understanding=qu_pin,
                )
                await self._recorder.persist(event)
                s.set_attribute("interaction_id", request.interaction_id)

            return response

    # ------------------------------------------------------------------
    # Generation prompt 합성(단일 프롬프트 — O×D resolver 미사용).
    # ------------------------------------------------------------------
    def _render_generation_prompt(self, query_text: str, pack: ContextPack) -> str:
        context_block = self._context_builder.render_for_prompt(pack)
        parts = [self._react_generation_source.prompt_body.strip()]
        if self._citation_contract:
            parts.append("# CITATION CONTRACT\n" + self._citation_contract.strip())
        parts.append("# CONTEXT\n" + context_block)
        parts.append("# QUERY\n" + query_text)
        # 출력-언어 trailer — # QUERY *뒤*(최고 recency)에 둔다. 생성 프롬프트 본문의
        # 언어 규칙은 영어 컨텍스트 블록보다 앞서므로 소형 모델이 영어 본문을 미러링할
        # 위험이 있다. v4 가 renderer trailer 를 query 뒤에 둔 것과 동일 lesson —
        # 번역 노드를 뺀 대신 프롬프트 *마지막*이 출력 언어를 가른다.
        parts.append(
            "# RESPONSE LANGUAGE\n"
            "Write the final answer in the same language as the QUERY above "
            "(Korean query → Korean answer). Citation markers and source ids stay verbatim."
        )
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 생성 후 검증 — 관측 전용(D1). 스트리밍은 되돌릴 수 없으므로 차단이 아니라 audit.
    # ------------------------------------------------------------------
    async def _verify(self, answer_text, citations, chunk_ids, ctx, record):
        citation_ids = [c.citation_id for c in citations]
        referenced = sorted(set(_CITE_RE.findall(answer_text)))
        cc = await self._tools.invoke(
            "verification.citation_check",
            {"answer_text": answer_text, "citation_ids": citation_ids,
             "chunk_ids": chunk_ids, "referenced_citation_ids": referenced},
            ctx,
        )
        record(cc)
        fc = await self._tools.invoke(
            "verification.faithfulness_check",
            {"answer_text": answer_text, "chunk_ids": chunk_ids}, ctx,
        )
        record(fc)
        cite_completeness = float((cc.output or {}).get("citation_completeness", 0.0))
        faithfulness = float((fc.output or {}).get("faithfulness", 0.0))
        # 관측 전용 판정: 둘 다 임계 이상이면 PASS, 아니면 PARTIAL(텍스트는 이미
        # 전송됐으므로 FAIL-차단은 의미 없다 — generation-verification coupling).
        if (cite_completeness >= self._cite_threshold
                and faithfulness >= self._faith_threshold):
            status = VerificationStatus.PASS.value
        else:
            status = VerificationStatus.PARTIAL.value
        return status, cite_completeness, faithfulness

    # ------------------------------------------------------------------
    # Generation — v4 패턴(LLM-agnostic, 스트리밍/non-streaming).
    # ------------------------------------------------------------------
    async def _generate(self, request, prompt_text, started, tool_calls, *,
                        llm: LLMPort, query_understanding: dict[str, Any] | None = None):
        with _TRACER.start_as_current_span("llm.generation") as s:
            em = current_emitter()
            try:
                if em.active:
                    llm_result = await self._generate_stream(llm, prompt_text, span=s)
                else:
                    llm_result = await llm.generate(prompt_text)
            except LLMUnavailableError:
                s.set_attribute("llm.status", "unavailable")
                return await self._refuse(
                    request, started, tool_calls, RefusalReason.LLM_UNAVAILABLE,
                    error_code="llm_unavailable", query_understanding=query_understanding,
                )
            s.set_attribute("model_id", llm_result.model_id)
            oi.set_kind(s, oi.KIND_LLM)
            oi.set_llm(
                s, model_name=llm_result.model_id, prompt=prompt_text,
                completion=llm_result.text,
                prompt_tokens=int(llm_result.token_usage.get("prompt_tokens", 0)),
                completion_tokens=int(llm_result.token_usage.get("completion_tokens", 0)),
            )
            return llm_result

    async def _generate_stream(self, llm: LLMPort, prompt: str, *, span) -> LLMResult:
        text_buf: list[str] = []
        token_usage: dict[str, int] = {}
        model_id: str | None = None
        async for delta in llm.generate_stream(prompt):
            if delta.content:
                text_buf.append(delta.content)
                await emit_token(delta.content)
            if delta.reasoning:
                await emit_reasoning(delta.reasoning)
            if delta.token_usage:
                token_usage = dict(delta.token_usage)
            if delta.model_id:
                model_id = delta.model_id
        return LLMResult(
            text="".join(text_buf),
            token_usage=token_usage or {"prompt_tokens": 0,
                                        "completion_tokens": len("".join(text_buf))},
            model_id=model_id or getattr(llm, "model_id", "unknown"),
        )

    # ------------------------------------------------------------------
    # 거부 — 1급 outcome(원칙 6). 분류기 인자 없음(scenario "n_a").
    # ------------------------------------------------------------------
    async def _refuse(self, request, started, tool_calls, reason: RefusalReason, *,
                      error_code: str | None,
                      query_understanding: dict[str, Any] | None = None,
                      corpus_map_hash: str | None = None,
                      scope_mode: str | None = None):
        await emit_step("refused", "ok", reason=reason.value)
        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text=_refusal_message(reason),
            citations=(),
            refusal_reason=reason.value,
            verification_status=VerificationStatus.SKIPPED.value,
            scenario_object="n_a",
            scenario_depth="n_a",
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage={},
            regulatory_grounding="n_a",
        )
        event = self._recorder.build(
            request=request, response=response, agent_variant=self.spec.variant_id,
            started_at=started, tool_calls=tuple(tool_calls), error_code=error_code,
            regulatory_grounding="n_a", query_understanding=query_understanding,
            corpus_map_hash=corpus_map_hash, scope_mode=scope_mode,
        )
        await self._recorder.persist(event)
        m = get_metrics()
        m.record_refusal(reason=reason.value)
        m.record_terminal(outcome="refused", latency_ms=response.latency_ms,
                          scenario_object="n_a", scenario_depth="n_a")
        return response


# submit_response.outcome → RefusalReason(answer 는 매핑 없음 → 생성 진행).
_OUTCOME_REFUSAL: dict[str, RefusalReason] = {
    "out_of_scope": RefusalReason.OUT_OF_SCOPE,
    "clarification": RefusalReason.CLARIFICATION_REQUIRED,
    "insufficient_evidence": RefusalReason.INSUFFICIENT_EVIDENCE,
}


def _to_citations(candidates) -> tuple[Citation, ...]:
    return tuple(
        Citation(
            citation_id=c.citation_id, chunk_id=c.chunk_id, document_id=c.document_id,
            page=c.page, score=c.score, doc_type=c.doc_type, section=c.section,
            revision=c.revision, response_date=c.response_date, formatted=c.formatted,
        )
        for c in candidates
    )


def _refusal_message(reason: RefusalReason) -> str:
    if reason is RefusalReason.CLARIFICATION_REQUIRED:
        return (
            "어떤 노형·규제에 대한 질문인지 명확히 해주세요. "
            "예: 노형명(NuScale, i-SMR), 규제 ID(RG 1.157, KINS-RG-...), RAI 번호."
        )
    if reason is RefusalReason.OUT_OF_SCOPE:
        return (
            "이 시스템은 SMR(소형모듈원자로) 인허가·원자력 규제 질의에 한해 "
            "검색 근거로 답변합니다. 해당 도메인의 노형·규제·RAI 관련 질문으로 "
            "다시 시도해 주세요. (법적·인허가 자문 권위를 대신하지 않습니다.)"
        )
    if reason is RefusalReason.LLM_UNAVAILABLE:
        return "응답이 지연되거나 모델을 가져올 수 없습니다. 잠시 후 다시 시도해 주세요."
    if reason is RefusalReason.RETRIEVAL_NO_RESULT:
        return "관련 근거를 검색에서 찾지 못했습니다. 질문을 더 구체적으로 다시 시도해 주세요."
    # INSUFFICIENT_EVIDENCE 및 기타.
    return "근거가 부족하여 답변을 제공할 수 없습니다."


@register_variant(REACT_MINIMAL_VARIANT_ID)
def _build_react_minimal(spec: VariantSpec, deps: AgentDeps) -> "ReactMinimalRunner":
    t = deps.tunables
    return ReactMinimalRunner(
        spec=spec,
        llm_router=deps.llm_router,
        tool_executor=deps.tool_executor,
        context_builder=deps.context_builder,
        recorder=deps.recorder,
        event_sink=deps.event_sink,
        app_profile=deps.app_profile,
        utility_llm=deps.utility_llm,
        react_retrieval_source=deps.react_retrieval_prompt_source,
        react_generation_source=deps.react_generation_prompt_source,
        citation_contract_path=t.get("citation_contract_path"),
        react_max_turns=t.get("react_max_turns", 8),
        verification_citation_threshold=t.get("verification_citation_threshold", 0.9),
        verification_faithfulness_threshold=t.get("verification_faithfulness_threshold", 0.85),
    )
