from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from app.application.agents.llm_router import LLMRouter, UnknownLLMError
from app.application.classification.active_cells import is_active
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder, sha256_hex
from app.application.memory.policies import decide_session_injection
from app.application.memory.summarizer import ConversationSummarizer
from app.application.prompting.renderer import PromptRenderer, RenderedPrompt
from app.application.prompting.resolver import PromptResolver
from app.application.tool_runtime.errors import RequiredToolFailed
from app.application.tool_runtime.executor import ToolExecutor
from app.domain.classification import DEFAULT_DEPTH, DEFAULT_OBJECT, ClassificationResult
from app.domain.errors import RefusalReason, VerificationStatus
from app.domain.interaction import (
    AgentRequest,
    AgentResponse,
    Citation,
    ChatTurn,
    ToolCallRecord,
)
from app.domain.memory import MemoryRef, MemoryReviewStatus, StalenessStatus
from app.domain.retrieval import RetrievedChunk
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")


class SequentialToolRoutedRunner:
    """v2 §7.1 — 15-step workflow. Every external capability is invoked via
    ToolExecutor. Node 1 classification + Node 4 verification fallback + Node 5
    multi-turn summary follow 기획 doc §Workflow."""

    variant_id = "sequential_tool_routed_v2"
    # None = pool 전체 호환. Router 풀에서 자동으로 등록된 LLM을 모두 사용 가능.
    compatible_llms: frozenset[str] | None = None

    def __init__(
        self,
        *,
        llm_router: LLMRouter,
        tool_executor: ToolExecutor,
        prompt_resolver: PromptResolver,
        prompt_renderer: PromptRenderer,
        context_builder: ContextBuilder,
        recorder: EventRecorder,
        event_sink: EventSinkPort,
        app_profile: str,
        classifier: Any | None = None,
        classification_threshold: float = 0.0,
        verification_citation_threshold: float = 0.5,
        verification_faithfulness_threshold: float = 0.5,
        verification_retry_on_fail: bool = False,
        summarizer: ConversationSummarizer | None = None,
    ) -> None:
        self._llm_router = llm_router
        self._tools = tool_executor
        self._resolver = prompt_resolver
        self._renderer = prompt_renderer
        self._context_builder = context_builder
        self._recorder = recorder
        self._sink = event_sink
        self._app_profile = app_profile
        self._classifier = classifier
        self._classification_threshold = classification_threshold
        self._cit_thr = verification_citation_threshold
        self._faith_thr = verification_faithfulness_threshold
        self._retry_on_fail = verification_retry_on_fail
        self._summarizer = summarizer

    async def run(self, request: AgentRequest) -> AgentResponse:
        started = time.monotonic()
        tool_calls: list[ToolCallRecord] = []

        # Pre-classification ctx (used only for tool calls that don't depend on O/D).
        ctx = ToolExecutionContext(
            interaction_id=request.interaction_id,
            trace_id="",
            app_profile=self._app_profile,
            agent_variant=self.variant_id,
            session_id=request.session_id,
            user_id=request.user_id,
            project_id=request.project_id,
        )

        def record(r) -> None:
            tool_calls.append(
                ToolCallRecord(
                    name=r.tool_name,
                    version=r.tool_version,
                    status=r.status,
                    latency_ms=r.latency_ms,
                    input_hash=r.input_hash,
                    output_hash=r.output_hash,
                    error_code=r.error_code,
                    retry_count=r.retry_count,
                )
            )

        try:
            llm_id, llm = self._llm_router.resolve(request.model or None)
        except UnknownLLMError:
            llm_id, llm = self._llm_router.resolve(None)

        with _TRACER.start_as_current_span("agent.run") as root:
            root.set_attribute("interaction_id", request.interaction_id)
            root.set_attribute("agent.variant", self.variant_id)
            root.set_attribute("llm_id", llm_id)
            root.set_attribute("session_id", request.session_id or "")

            # === Node 1: intent_classification ===
            with _TRACER.start_as_current_span("agent.intent_classification") as s:
                classification = await self._classify(request)
                scenario_object = classification.scenario_object
                scenario_depth = classification.scenario_depth
                entities = classification.entities
                classification_confidence = classification.confidence
                s.set_attribute("scenario_object", scenario_object)
                s.set_attribute("scenario_depth", scenario_depth)
                s.set_attribute("classification_confidence", classification_confidence)
                s.set_attribute("classifier_backend", classification.classifier_backend)
                if entities:
                    s.set_attribute("entity_kinds", ",".join(sorted(entities.keys())))

            # Refuse early on low-confidence or inactive cells.
            if (
                self._classifier is not None
                and classification_confidence < self._classification_threshold
            ):
                return await self._refuse(
                    request,
                    started,
                    tool_calls,
                    scenario_object,
                    scenario_depth,
                    RefusalReason.CLARIFICATION_REQUIRED,
                    classification_confidence,
                    verification_status=VerificationStatus.SKIPPED,
                    error_code="classification_low_confidence",
                )
            if not is_active(scenario_object, scenario_depth):
                return await self._refuse(
                    request,
                    started,
                    tool_calls,
                    scenario_object,
                    scenario_depth,
                    RefusalReason.UNSUPPORTED_SCENARIO,
                    classification_confidence,
                    verification_status=VerificationStatus.SKIPPED,
                    error_code="inactive_cell",
                )

            # Rebuild ctx with classified O/D for downstream tool calls.
            ctx = replace(ctx, scenario_object=scenario_object, scenario_depth=scenario_depth)

            # === Node 2 pre-step: scenario_routing (logical) ===
            with _TRACER.start_as_current_span("agent.scenario_routing"):
                pass

            # === 3. tool.memory.session_load ===
            session_load = await self._tools.invoke(
                "memory.session_load",
                {"session_id": request.session_id},
                ctx,
            )
            record(session_load)

            prior_so = None
            prior_sd = None
            prior_entities: dict[str, list[str]] = {}
            conversation_summary: str | None = None
            if session_load.output and session_load.output.get("present"):
                prior_so = session_load.output.get("active_scenario_object")
                prior_sd = session_load.output.get("active_scenario_depth")
                prior_entities = session_load.output.get("active_entities") or {}
                conversation_summary = session_load.output.get("conversation_summary")

            decision = decide_session_injection(
                has_chat_history=bool(request.chat_history),
                prior_scenario_object=prior_so,
                prior_scenario_depth=prior_sd,
                current_scenario_object=scenario_object,
                current_scenario_depth=scenario_depth,
                prior_entities=prior_entities,
                current_entities=entities,
            )
            root.set_attribute("session_injection", decision.inject)
            root.set_attribute("session_injection_reason", decision.reason)

            memory_refs: tuple[MemoryRef, ...] = ()
            memory_ids_used: list[str] = []
            memory_types_used: list[str] = []
            memory_review_statuses: dict[str, str] = {}
            memory_staleness_statuses: dict[str, str] = {}
            if decision.inject and session_load.output and session_load.output.get("present"):
                memory_ids_used.append(request.session_id or "")
                memory_types_used.append("session")
                memory_review_statuses[request.session_id or ""] = MemoryReviewStatus.APPROVED.value
                memory_staleness_statuses[request.session_id or ""] = StalenessStatus.FRESH.value
                memory_refs = (
                    MemoryRef(
                        memory_id=request.session_id or "",
                        memory_type="session",
                        review_status=MemoryReviewStatus.APPROVED.value,
                        staleness_status=StalenessStatus.FRESH.value,
                    ),
                )

            # === 4. tool.retriever.search (Node 2) ===
            try:
                retrieval = await self._tools.invoke(
                    "retriever.search",
                    {
                        "query_text": request.query_text,
                        "top_k": 3,
                        "scenario_object": scenario_object,
                        "scenario_depth": scenario_depth,
                        "entities": entities,
                    },
                    ctx,
                )
            except RequiredToolFailed as e:
                return await self._refuse(
                    request,
                    started,
                    tool_calls,
                    scenario_object,
                    scenario_depth,
                    RefusalReason.RETRIEVAL_NO_RESULT,
                    classification_confidence,
                    error_code=e.code.value,
                )
            record(retrieval)
            raw_chunks = (retrieval.output or {}).get("chunks", [])
            chunks = [
                RetrievedChunk(
                    chunk_id=c["chunk_id"],
                    document_id=c["document_id"],
                    score=c["score"],
                    page=c.get("page"),
                    section=c.get("section"),
                    snippet=c.get("snippet"),
                    doc_type=c.get("doc_type"),
                    revision=c.get("revision"),
                    response_date=c.get("response_date"),
                )
                for c in raw_chunks
            ]

            # === 5. tool.memory.approved_search (Phase 5에서 활성화) ===
            approved = await self._tools.invoke(
                "memory.approved_search",
                {
                    "query_text": request.query_text,
                    "scenario_object": scenario_object,
                    "scenario_depth": scenario_depth,
                    "top_k": 5,
                },
                ctx,
            )
            record(approved)

            # === Track E: summary compression (Node 5 책임이지만 prompt에 prepend하기 위해 여기서 갱신) ===
            if self._summarizer is not None:
                summ = await self._summarizer.summarize(
                    prior_summary=conversation_summary,
                    chat_history=request.chat_history,
                )
                conversation_summary = summ.summary or conversation_summary
                root.set_attribute("summary.compressed", summ.compressed)
                root.set_attribute("summary.reason", summ.reason)

            # === 6. context_building ===
            with _TRACER.start_as_current_span("agent.context_build") as s:
                pack = self._context_builder.build(
                    interaction_id=request.interaction_id,
                    query_text=request.query_text,
                    chat_history=request.chat_history,
                    conversation_summary=conversation_summary if decision.inject else None,
                    scenario_object=scenario_object,
                    scenario_depth=scenario_depth,
                    entities=entities,
                    chunks=chunks,
                    memory_refs=memory_refs,
                )
                s.set_attribute("context_hash", pack.context_hash)

            # === 7. prompt_rendering ===
            with _TRACER.start_as_current_span("agent.prompt_render") as s:
                profile = self._resolver.resolve(scenario_object, scenario_depth)
                if profile is None:
                    rendered = RenderedPrompt(
                        profile_id="fallback",
                        version="v0",
                        text=f"{request.query_text}\n\n[no profile resolved]",
                        hash=sha256_hex(request.query_text),
                        fragments={},
                    )
                else:
                    context_block = self._context_builder.render_for_prompt(pack)
                    rendered = self._renderer.render(
                        profile,
                        query_text=request.query_text,
                        context_block=context_block,
                    )
                s.set_attribute("prompt_profile_id", rendered.profile_id)
                s.set_attribute("prompt_version", rendered.version)
                s.set_attribute("rendered_prompt_hash", rendered.hash)
                await self._sink.write_prompt_render_record(
                    request.interaction_id,
                    self._renderer.to_record(rendered, query_text=request.query_text),
                )
                await self._sink.write_context_snapshot(
                    request.interaction_id, self._context_builder.to_snapshot(pack)
                )

            # === 8. generation (Node 3) ===
            llm_result = await self._generate(request, rendered, started, tool_calls,
                                              scenario_object, scenario_depth,
                                              classification_confidence, llm=llm)
            if isinstance(llm_result, AgentResponse):
                return llm_result  # LLM unavailable refusal

            citation_ids = [c.citation_id for c in pack.citation_candidates]
            chunk_ids = [c.chunk_id for c in chunks]

            # === 9. tool.document.resolve_citation ===
            try:
                resolve = await self._tools.invoke(
                    "document.resolve_citation",
                    {"citation_ids": citation_ids, "chunk_ids": chunk_ids},
                    ctx,
                )
            except RequiredToolFailed as e:
                return await self._refuse(
                    request,
                    started,
                    tool_calls,
                    scenario_object,
                    scenario_depth,
                    RefusalReason.VERIFICATION_FAILED,
                    classification_confidence,
                    error_code=e.code.value,
                )
            record(resolve)

            # === 10–11. verification (Node 4) + 1차 실패 fallback ===
            citation_completeness, faithfulness, verification_status, llm_result, retry_count = (
                await self._verify_with_fallback(
                    request=request,
                    rendered=rendered,
                    llm_result=llm_result,
                    citation_ids=citation_ids,
                    chunk_ids=chunk_ids,
                    ctx=ctx,
                    record=record,
                    llm=llm,
                )
            )
            root.set_attribute("verification.retry_count", retry_count)

            # === 12. memory_candidate_extract (Phase 4) ===
            with _TRACER.start_as_current_span("memory.candidate_extract"):
                pass

            # === 13. tool.memory.session_update ===
            new_turns = list(request.chat_history) + [
                ChatTurn(role="user", content=request.query_text)
            ]
            session_update = await self._tools.invoke(
                "memory.session_update",
                {
                    "session_id": request.session_id or "",
                    "recent_turns": [{"role": t.role, "content": t.content} for t in new_turns][-10:],
                    "active_entities": entities,
                    "active_scenario_object": scenario_object,
                    "active_scenario_depth": scenario_depth,
                    "conversation_summary": conversation_summary or "",
                    "last_retrieved_chunk_ids": chunk_ids,
                    "last_memory_ids_used": memory_ids_used,
                },
                ctx,
            )
            record(session_update)

            # === 14. tool.artifact.write_event ===
            persist_tool = await self._tools.invoke(
                "artifact.write_event",
                {
                    "interaction_id": request.interaction_id,
                    "event_kind": "interaction",
                    "payload": {"variant": self.variant_id},
                },
                ctx,
            )
            record(persist_tool)

            # === 15. response_formatting (Node 5) ===
            with _TRACER.start_as_current_span("agent.response_format"):
                if verification_status == VerificationStatus.FAIL.value:
                    refusal = RefusalReason.VERIFICATION_FAILED.value
                    answer_text = "근거가 부족하여 답변을 제공할 수 없습니다."
                    citations: tuple[Citation, ...] = ()
                elif verification_status == VerificationStatus.PARTIAL.value:
                    refusal = RefusalReason.PARTIAL_ANSWER.value
                    answer_text = (
                        llm_result.text
                        + "\n\n[부분 답변] 일부 인용·근거가 임계값을 충족하지 못했습니다."
                    )
                    citations = tuple(
                        Citation(
                            citation_id=c.citation_id,
                            chunk_id=c.chunk_id,
                            document_id=c.document_id,
                            page=c.page,
                            score=c.score,
                            doc_type=c.doc_type,
                            section=c.section,
                            revision=c.revision,
                            response_date=c.response_date,
                            formatted=c.formatted,
                        )
                        for c in pack.citation_candidates
                    )
                else:
                    refusal = None
                    answer_text = llm_result.text
                    citations = tuple(
                        Citation(
                            citation_id=c.citation_id,
                            chunk_id=c.chunk_id,
                            document_id=c.document_id,
                            page=c.page,
                            score=c.score,
                            doc_type=c.doc_type,
                            section=c.section,
                            revision=c.revision,
                            response_date=c.response_date,
                            formatted=c.formatted,
                        )
                        for c in pack.citation_candidates
                    )
                response = AgentResponse(
                    interaction_id=request.interaction_id,
                    answer_text=answer_text,
                    citations=citations,
                    refusal_reason=refusal,
                    verification_status=verification_status,
                    scenario_object=scenario_object,
                    scenario_depth=scenario_depth,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    token_usage=dict(llm_result.token_usage),
                    classification_confidence=classification_confidence,
                    classifier_backend=classification.classifier_backend,
                    entities=entities,
                    llm_id=llm_id,
                    model_id=llm_result.model_id,
                )

            with _TRACER.start_as_current_span("event.persist") as s:
                event = self._recorder.build(
                    request=request,
                    response=response,
                    agent_variant=self.variant_id,
                    retrieved_chunk_ids=tuple(chunk_ids),
                    retrieval_confidence=chunks[0].score if chunks else 0.0,
                    prompt_profile_id=rendered.profile_id,
                    prompt_version=rendered.version,
                    rendered_prompt_hash=rendered.hash,
                    context_hash=pack.context_hash,
                    classification_confidence=classification_confidence,
                    citation_completeness=citation_completeness,
                    faithfulness=faithfulness,
                    started_at=started,
                    tool_calls=tuple(tool_calls),
                    memory_ids_used=tuple(memory_ids_used),
                    memory_types_used=tuple(memory_types_used),
                    memory_review_statuses=memory_review_statuses,
                    memory_staleness_statuses=memory_staleness_statuses,
                )
                await self._recorder.persist(event)
                s.set_attribute("interaction_id", request.interaction_id)

            root.set_attribute("verification_status", verification_status)
            root.set_attribute("latency_ms", response.latency_ms)

        return response

    # ----------------------------------------------------------------------
    # Node 1 — classification
    # ----------------------------------------------------------------------
    async def _classify(self, request: AgentRequest) -> ClassificationResult:
        if self._classifier is None:
            # Legacy behavior: hardcoded O4/D2 (used by unit tests that don't inject a classifier).
            return ClassificationResult(
                scenario_object=DEFAULT_OBJECT,
                scenario_depth=DEFAULT_DEPTH,
                entities={},
                confidence=0.5,
                object_confidence=0.5,
                depth_confidence=0.5,
                classifier_backend="hardcoded",
            )
        return await self._classifier.classify(request.query_text, request.chat_history)

    # ----------------------------------------------------------------------
    # Node 3 — generation
    # ----------------------------------------------------------------------
    async def _generate(
        self,
        request,
        rendered: RenderedPrompt,
        started: float,
        tool_calls: list,
        scenario_object: str,
        scenario_depth: str,
        classification_confidence: float,
        *,
        llm: LLMPort,
    ):
        with _TRACER.start_as_current_span("llm.generation") as s:
            try:
                llm_result = await llm.generate(rendered.text)
            except LLMUnavailableError as exc:
                s.set_attribute("llm.error", str(exc)[:256])
                s.set_attribute("llm.status", "unavailable")
                return await self._refuse(
                    request,
                    started,
                    tool_calls,
                    scenario_object,
                    scenario_depth,
                    RefusalReason.LLM_UNAVAILABLE,
                    classification_confidence,
                    error_code="llm_unavailable",
                )
            s.set_attribute("model_id", llm_result.model_id)
            s.set_attribute(
                "prompt_tokens", llm_result.token_usage.get("prompt_tokens", 0)
            )
            s.set_attribute(
                "completion_tokens", llm_result.token_usage.get("completion_tokens", 0)
            )
            return llm_result

    # ----------------------------------------------------------------------
    # Node 4 — verification with retry fallback
    # ----------------------------------------------------------------------
    async def _verify_with_fallback(
        self,
        *,
        request,
        rendered: RenderedPrompt,
        llm_result,
        citation_ids: list[str],
        chunk_ids: list[str],
        ctx: ToolExecutionContext,
        record,
        llm: LLMPort,
    ):
        retry_count = 0

        async def _run_checks(answer_text: str):
            try:
                cit = await self._tools.invoke(
                    "verification.citation_check",
                    {
                        "answer_text": answer_text,
                        "citation_ids": citation_ids,
                        "chunk_ids": chunk_ids,
                    },
                    ctx,
                )
            except RequiredToolFailed:
                raise
            record(cit)
            cc = float((cit.output or {}).get("citation_completeness", 0.0))
            try:
                f = await self._tools.invoke(
                    "verification.faithfulness_check",
                    {"answer_text": answer_text, "chunk_ids": chunk_ids},
                    ctx,
                )
            except RequiredToolFailed:
                raise
            record(f)
            fh = float((f.output or {}).get("faithfulness", 0.0))
            return cc, fh

        citation_completeness, faithfulness = await _run_checks(llm_result.text)
        ok = (
            citation_completeness >= self._cit_thr
            and faithfulness >= self._faith_thr
        )

        if not ok and self._retry_on_fail:
            # Node 3 fallback — 한 번만 재시도, temperature는 LLM 어댑터에 일임.
            retry_count = 1
            with _TRACER.start_as_current_span("llm.generation.retry") as s:
                try:
                    llm_result = await llm.generate(
                        rendered.text,
                        model_options={"temperature": 0.0},
                    )
                except LLMUnavailableError:
                    s.set_attribute("llm.status", "unavailable")
                else:
                    citation_completeness, faithfulness = await _run_checks(
                        llm_result.text
                    )
                    ok = (
                        citation_completeness >= self._cit_thr
                        and faithfulness >= self._faith_thr
                    )

        if ok:
            return (
                citation_completeness,
                faithfulness,
                VerificationStatus.PASS.value,
                llm_result,
                retry_count,
            )
        # 2차 실패 → 부분 답변 vs 완전 거부 결정. 임계값의 절반은 넘으면 partial.
        partial_ok = (
            citation_completeness >= self._cit_thr * 0.5
            and faithfulness >= self._faith_thr * 0.5
        )
        status = (
            VerificationStatus.PARTIAL.value
            if partial_ok
            else VerificationStatus.FAIL.value
        )
        return citation_completeness, faithfulness, status, llm_result, retry_count

    # ----------------------------------------------------------------------
    # Refusal path
    # ----------------------------------------------------------------------
    async def _refuse(
        self,
        request: AgentRequest,
        started: float,
        tool_calls: list[ToolCallRecord],
        scenario_object: str,
        scenario_depth: str,
        reason: RefusalReason,
        classification_confidence: float,
        *,
        error_code: str | None,
        verification_status: VerificationStatus = VerificationStatus.FAIL,
    ) -> AgentResponse:
        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text=_refusal_message(reason),
            citations=(),
            refusal_reason=reason.value,
            verification_status=verification_status.value,
            scenario_object=scenario_object,
            scenario_depth=scenario_depth,
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage={},
        )
        event = self._recorder.build(
            request=request,
            response=response,
            agent_variant=self.variant_id,
            started_at=started,
            tool_calls=tuple(tool_calls),
            classification_confidence=classification_confidence,
            error_code=error_code,
        )
        await self._recorder.persist(event)
        return response


def _refusal_message(reason: RefusalReason) -> str:
    if reason is RefusalReason.CLARIFICATION_REQUIRED:
        return "질문을 조금 더 구체화해 주세요. 분류 신뢰도가 낮습니다."
    if reason is RefusalReason.UNSUPPORTED_SCENARIO:
        return "현재 단계에서는 답변할 수 없는 시나리오입니다."
    if reason is RefusalReason.LLM_UNAVAILABLE:
        return "모델 응답을 가져올 수 없습니다. 잠시 후 다시 시도해 주세요."
    return "근거가 부족하여 답변을 제공할 수 없습니다."
