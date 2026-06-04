from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import replace
from typing import Any, AsyncIterator

from app.application.agents.events import (
    AgentEvent,
    EventEmitter,
    bind_emitter,
    current_emitter,
    emit_reasoning,
    emit_step,
    emit_step_nowait,
    emit_token,
    emit_tool_nowait,
    unbind_emitter,
)
from app.application.agents.llm_router import LLMRouter, UnknownLLMError
from app.application.agents.registry import AgentDeps, register_variant
from app.application.classification.active_cells import is_active
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.memory.policies import decide_session_injection
from app.application.memory.summarizer import ConversationSummarizer
from app.application.prompting.renderer import PromptRenderer, RenderedPrompt
from app.application.prompting.resolver import PromptResolver
from app.application.tool_runtime.errors import RequiredToolFailed
from app.application.tool_runtime.executor import ToolExecutor
from app.domain.agents import VariantSpec
from app.domain.classification import DEFAULT_DEPTH, DEFAULT_OBJECT, ClassificationResult
from app.domain.errors import (
    PromptProfileNotFoundError,
    RefusalReason,
    VerificationStatus,
)
from app.domain.interaction import (
    AgentRequest,
    AgentResponse,
    Citation,
    ChatTurn,
    ToolCallRecord,
)
from app.domain.memory import MemoryRef, MemoryReviewStatus, StalenessStatus
from app.domain.retrieval import RetrieverSearchOutput
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMResult, LLMTokenDelta, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

# `[cite-N]` marker — runner extracts referenced citation ids from answer_text
# so verification.citation_check sees what the LLM actually cited.
_CITE_PATTERN = re.compile(r"\[(cite-\d+)\]")


class SequentialToolRoutedRunner:
    """v2 §7.1 — 15-step workflow. Every external capability is invoked via
    ToolExecutor. Node 1 classification + Node 4 verification fallback + Node 5
    multi-turn summary follow 기획 doc §Workflow."""

    def __init__(
        self,
        *,
        spec: VariantSpec,
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
        retriever_top_k: int = 3,
        retriever_min_score: float = 0.0,
        active_cells_mode: str = "all",
    ) -> None:
        self.spec = spec
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
        self._top_k = retriever_top_k
        self._min_score = retriever_min_score
        self._active_cells_mode = active_cells_mode

    async def run_stream(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentEvent]:
        """Async-iterator counterpart to `run()` — yields progress events
        (step / tool / token / reasoning) as the 15-step workflow advances,
        and a terminal `final` event carrying the same `AgentResponse` that
        `run()` would return. SSE layer consumes this directly.

        Implementation note: `run()` itself is wrapped in an asyncio task,
        with an EventEmitter installed on the current context (propagated
        into the task by asyncio's contextvar copy semantics). The emitter
        is queue-backed, so `record()` and the per-node helpers can publish
        synchronously without `await`.
        """
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
                payload={"message": str(run_error),
                         "type": type(run_error).__name__},
                ts=time.monotonic(),
            )
            return
        if response is not None:
            yield AgentEvent(
                kind="final",
                payload={"response": response},
                ts=time.monotonic(),
            )

    async def run(self, request: AgentRequest) -> AgentResponse:
        started = time.monotonic()
        tool_calls: list[ToolCallRecord] = []
        tool_result_refs: list[str] = []

        # Pre-classification ctx (used only for tool calls that don't depend on O/D).
        ctx = ToolExecutionContext(
            interaction_id=request.interaction_id,
            trace_id="",
            app_profile=self._app_profile,
            agent_variant=self.spec.variant_id,
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
            if r.output_hash:
                tool_result_refs.append(r.output_hash)
            # Sidechannel emit for SSE — no-op when no emitter is bound.
            emit_tool_nowait(
                r.tool_name,
                r.status,
                version=r.tool_version,
                latency_ms=r.latency_ms,
                error_code=r.error_code,
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
            root.set_attribute("session_id", request.session_id or "")
            oi.set_kind(root, oi.KIND_AGENT)
            oi.set_io(root, input_value=request.query_text)

            # === Node 1: intent_classification ===
            await emit_step(
                "intent_classification",
                "started",
                query=request.query_text[:200],
            )
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
                oi.set_kind(s, oi.KIND_CHAIN)
                oi.set_io(
                    s,
                    input_value=request.query_text,
                    output_value={
                        "scenario_object": scenario_object,
                        "scenario_depth": scenario_depth,
                        "confidence": classification_confidence,
                        "entities": entities,
                        "classifier_backend": classification.classifier_backend,
                    },
                )
            await emit_step(
                "intent_classification",
                "ok",
                scenario_object=scenario_object,
                scenario_depth=scenario_depth,
                confidence=classification_confidence,
                classifier_backend=classification.classifier_backend,
                entities=entities,
            )

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
            if not is_active(scenario_object, scenario_depth, mode=self._active_cells_mode):
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
            await emit_step("session_memory_load", "started")
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

            summary_preview = (conversation_summary or "")[:200] if conversation_summary else None
            await emit_step(
                "session_memory_load",
                "ok",
                present=bool(session_load.output and session_load.output.get("present")),
                injected=decision.inject,
                reason=decision.reason,
                prior_scenario_object=prior_so,
                prior_scenario_depth=prior_sd,
                summary_preview=summary_preview,
            )

            # === 4. tool.retriever.search (Node 2) ===
            await emit_step(
                "retrieval",
                "started",
                query=request.query_text[:200],
                top_k=self._top_k,
            )
            try:
                retrieval = await self._tools.invoke(
                    "retriever.search",
                    {
                        "query_text": request.query_text,
                        "top_k": self._top_k,
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
            retrieval_output = RetrieverSearchOutput.model_validate(retrieval.output or {})
            chunks = [c for c in retrieval_output.chunks if c.score >= self._min_score]
            if not chunks:
                return await self._refuse(
                    request,
                    started,
                    tool_calls,
                    scenario_object,
                    scenario_depth,
                    RefusalReason.RETRIEVAL_NO_RESULT,
                    classification_confidence,
                    error_code="tool_empty_result",
                )

            chunks_preview = [
                {
                    "chunk_id": c.chunk_id,
                    "document_id": c.document_id,
                    "title": c.title,
                    "page": c.page,
                    "section": c.section,
                    "score": c.score,
                    "doc_type": c.doc_type,
                    "snippet": (c.snippet or c.text or "")[:200] or None,
                }
                for c in chunks
            ]
            await emit_step(
                "retrieval",
                "ok",
                num_chunks=len(chunks),
                chunks_preview=chunks_preview,
            )

            # === 5. tool.memory.approved_search (Phase 5에서 활성화) ===
            await emit_step("memory_approved_search", "started")
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
            memory_retrieval_scores: dict[str, float] = {}
            approved_refs: list[MemoryRef] = []
            for hit in (approved.output or {}).get("hits", []) or []:
                mid = hit.get("memory_id")
                if not mid:
                    continue
                score = float(hit.get("score", 0.0))
                memory_retrieval_scores[mid] = score
                memory_ids_used.append(mid)
                memory_types_used.append("approved")
                memory_review_statuses[mid] = MemoryReviewStatus.APPROVED.value
                memory_staleness_statuses[mid] = StalenessStatus.FRESH.value
                approved_refs.append(
                    MemoryRef(
                        memory_id=mid,
                        memory_type="approved",
                        review_status=MemoryReviewStatus.APPROVED.value,
                        staleness_status=StalenessStatus.FRESH.value,
                    )
                )
            if approved_refs:
                memory_refs = memory_refs + tuple(approved_refs)

            hits_preview = [
                {
                    "memory_id": ref.memory_id,
                    "score": memory_retrieval_scores.get(ref.memory_id),
                }
                for ref in approved_refs
            ]
            await emit_step(
                "memory_approved_search",
                "ok",
                hit_count=len(approved_refs),
                hits_preview=hits_preview,
            )

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
            await emit_step("context_build", "started")
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
                    tool_result_refs=tuple(tool_result_refs),
                )
                s.set_attribute("context_hash", pack.context_hash)
                oi.set_kind(s, oi.KIND_RETRIEVER)
                oi.set_retrieval_documents(
                    s,
                    [
                        {
                            "id": c.chunk_id,
                            "score": c.score,
                            "content": getattr(c, "text", None) or getattr(c, "content", ""),
                            "metadata": {
                                "document_id": getattr(c, "document_id", None),
                                "page": getattr(c, "page", None),
                                "section": getattr(c, "section", None),
                                "doc_type": getattr(c, "doc_type", None),
                            },
                        }
                        for c in chunks
                    ],
                )
                oi.set_io(
                    s,
                    input_value=request.query_text,
                    output_value={
                        "context_hash": pack.context_hash,
                        "num_chunks": len(chunks),
                        "num_memory_refs": len(memory_refs),
                    },
                )

            await emit_step("context_build", "ok", context_hash=pack.context_hash)

            # === 7. prompt_rendering ===
            await emit_step("prompt_render", "started")
            with _TRACER.start_as_current_span("agent.prompt_render") as s:
                try:
                    profile = self._resolver.resolve(scenario_object, scenario_depth)
                except PromptProfileNotFoundError as exc:
                    s.set_attribute("prompt.resolved", False)
                    s.set_attribute("prompt.miss_scenario_object", exc.scenario_object)
                    s.set_attribute("prompt.miss_scenario_depth", exc.scenario_depth)
                    return await self._refuse(
                        request,
                        started,
                        tool_calls,
                        scenario_object,
                        scenario_depth,
                        RefusalReason.UNKNOWN_SCENARIO,
                        classification_confidence,
                        verification_status=VerificationStatus.SKIPPED,
                        error_code="prompt_profile_not_found",
                    )
                context_block = self._context_builder.render_for_prompt(pack)
                rendered = self._renderer.render(
                    profile,
                    query_text=request.query_text,
                    context_block=context_block,
                )
                s.set_attribute("prompt_profile_id", rendered.profile_id)
                s.set_attribute("prompt_version", rendered.profile_version)
                s.set_attribute("prompt_source", rendered.source)
                s.set_attribute("rendered_prompt_hash", rendered.rendered_prompt_hash)
                s.set_attribute("prompt_composition_hash", rendered.composition_hash)
                for name, sha in rendered.fragment_hashes.items():
                    s.set_attribute(f"prompt.fragment.{name}.sha", sha[:16])
                    s.set_attribute(
                        f"prompt.fragment.{name}.version",
                        rendered.fragment_versions.get(name, ""),
                    )
                oi.set_kind(s, oi.KIND_CHAIN)
                oi.set_io(
                    s,
                    input_value={
                        "query_text": request.query_text,
                        "context_block_len": len(context_block),
                        "profile_id": rendered.profile_id,
                        "profile_version": rendered.profile_version,
                    },
                    output_value=rendered.text,
                )
                await self._sink.write_prompt_render_record(
                    request.interaction_id,
                    self._renderer.to_record(rendered, query_text=request.query_text),
                )
                await self._sink.write_context_snapshot(
                    request.interaction_id, self._context_builder.to_snapshot(pack)
                )

            await emit_step("prompt_render", "ok",
                            profile_id=rendered.profile_id,
                            profile_version=rendered.profile_version)

            # === 8. generation (Node 3) ===
            await emit_step("generation", "started", llm_id=llm_id)
            llm_result = await self._generate(request, rendered, started, tool_calls,
                                              scenario_object, scenario_depth,
                                              classification_confidence, llm=llm)
            if isinstance(llm_result, AgentResponse):
                return llm_result  # LLM unavailable refusal
            await emit_step(
                "generation", "ok",
                completion_tokens=llm_result.token_usage.get("completion_tokens", 0),
            )

            citation_ids = [c.citation_id for c in pack.citation_candidates]
            chunk_ids = [c.chunk_id for c in chunks]

            # === 9. tool.document.resolve_citation ===
            await emit_step("citation_resolve", "started")
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

            # Overlay resolve output onto candidate metadata (Node 9 — feed
            # authoritative doc info to verification + response).
            resolved_by_cid: dict[str, dict[str, Any]] = {
                r.get("citation_id"): r
                for r in (resolve.output or {}).get("resolved", []) or []
                if r.get("citation_id")
            }
            final_candidates = tuple(
                replace(
                    c,
                    document_id=(resolved_by_cid.get(c.citation_id) or {}).get(
                        "document_id"
                    )
                    or c.document_id,
                    page=(resolved_by_cid.get(c.citation_id) or {}).get("page")
                    or c.page,
                    section=(resolved_by_cid.get(c.citation_id) or {}).get("section")
                    or c.section,
                    revision=(resolved_by_cid.get(c.citation_id) or {}).get("revision")
                    or c.revision,
                )
                for c in pack.citation_candidates
            )
            resolvable_citation_ids = [
                cid
                for cid, r in resolved_by_cid.items()
                if r.get("resolvable", False)
            ]
            resolved_preview = [
                {
                    "citation_id": cid,
                    "document_id": (resolved_by_cid.get(cid) or {}).get("document_id"),
                    "page": (resolved_by_cid.get(cid) or {}).get("page"),
                    "section": (resolved_by_cid.get(cid) or {}).get("section"),
                    "revision": (resolved_by_cid.get(cid) or {}).get("revision"),
                }
                for cid in resolvable_citation_ids
            ]
            await emit_step(
                "citation_resolve",
                "ok",
                resolved_count=len(resolvable_citation_ids),
                total=len(citation_ids),
                resolved_preview=resolved_preview,
            )

            # === 10–11. verification (Node 4) + 1차 실패 fallback ===
            await emit_step("verification", "started")
            citation_completeness, faithfulness, verification_status, llm_result, retry_count = (
                await self._verify_with_fallback(
                    request=request,
                    rendered=rendered,
                    llm_result=llm_result,
                    citation_ids=citation_ids,
                    chunk_ids=chunk_ids,
                    resolvable_citation_ids=resolvable_citation_ids,
                    ctx=ctx,
                    record=record,
                    llm=llm,
                )
            )
            root.set_attribute("verification.retry_count", retry_count)
            await emit_step(
                "verification", "ok",
                verification_status=verification_status,
                citation_completeness=citation_completeness,
                faithfulness=faithfulness,
                retry_count=retry_count,
            )

            # === 12. memory_candidate_extract (Phase 4) ===
            with _TRACER.start_as_current_span("memory.candidate_extract"):
                pass

            # === 13. tool.memory.session_update ===
            new_turns = list(request.chat_history) + [
                ChatTurn(role="user", content=request.query_text)
            ]
            await emit_step("session_memory_update", "started")
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
            await emit_step("session_memory_update", "ok", tool_status=session_update.status)

            # === 14. event.persist (Node 14 — recorder is the sole artifact
            # write path; v2 spec §15 단일 sink). artifact.write_event tool 제거.
            # 실제 persist는 아래 `event.persist` span에서 수행됨.

            # === 15. response_formatting (Node 5) ===
            with _TRACER.start_as_current_span("agent.response_format") as _rfmt:
                if verification_status == VerificationStatus.FAIL.value:
                    refusal = RefusalReason.VERIFICATION_FAILED.value
                    answer_text = _refusal_message(RefusalReason.VERIFICATION_FAILED)
                    citations: tuple[Citation, ...] = ()
                elif verification_status == VerificationStatus.PARTIAL.value:
                    refusal = RefusalReason.PARTIAL_ANSWER.value
                    # 부분 답변 고지는 answer_text 에 baking 하지 않는다(decision A) —
                    # API boundary(answer_renderer)가 verification_status 에서 마크다운
                    # callout 을 content 로 합성. 구조화 필드가 단일 표현 소스.
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
                        for c in final_candidates
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
                        for c in final_candidates
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
                oi.set_kind(_rfmt, oi.KIND_CHAIN)
                oi.set_io(
                    _rfmt,
                    input_value={
                        "verification_status": verification_status,
                        "refusal": refusal,
                    },
                    output_value={
                        "answer_text": answer_text,
                        "num_citations": len(citations),
                    },
                )

            with _TRACER.start_as_current_span("event.persist") as s:
                event = self._recorder.build(
                    request=request,
                    response=response,
                    agent_variant=self.spec.variant_id,
                    retrieved_chunk_ids=tuple(chunk_ids),
                    retrieval_confidence=chunks[0].score if chunks else 0.0,
                    prompt_profile_id=rendered.profile_id,
                    prompt_version=rendered.profile_version,
                    rendered_prompt_hash=rendered.rendered_prompt_hash,
                    prompt_composition_hash=rendered.composition_hash,
                    prompt_fragment_versions=dict(rendered.fragment_versions),
                    prompt_source=rendered.source,
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
                    memory_retrieval_scores=memory_retrieval_scores,
                )
                await self._recorder.persist(event)
                s.set_attribute("interaction_id", request.interaction_id)
                # Deterministic artifact paths (see adapters/event_sink/*). UI
                # tooling can resolve these against MinIO/filesystem root.
                day = time.strftime("%Y-%m-%d", time.gmtime(started))
                s.set_attribute(
                    "artifact.interaction_events.key",
                    f"interaction_events/{day}/events.jsonl",
                )
                s.set_attribute(
                    "artifact.context_snapshot.key",
                    f"context_snapshots/{day}/{request.interaction_id}.json",
                )
                s.set_attribute(
                    "artifact.prompt_render_record.key",
                    f"prompt_render_records/{day}/{request.interaction_id}.json",
                )

            root.set_attribute("verification_status", verification_status)
            root.set_attribute("latency_ms", response.latency_ms)
            oi.set_io(root, output_value=response.answer_text)

        return response

    # ----------------------------------------------------------------------
    # Node 1 — classification
    # ----------------------------------------------------------------------
    async def _classify(self, request: AgentRequest) -> ClassificationResult:
        # ADR-0003: Node 1 logic lives in `sequential/nodes/classify.py`.
        # This thin shim keeps the conductor's call sites unchanged while
        # node extraction proceeds in follow-up PRs.
        from app.application.agents.sequential.nodes.classify import classify

        return await classify(request, self._classifier)

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
            em = current_emitter()
            try:
                if em.active:
                    llm_result = await self._generate_stream(llm, rendered.text, span=s)
                else:
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
            oi.set_kind(s, oi.KIND_LLM)
            oi.set_llm(
                s,
                model_name=llm_result.model_id,
                prompt=rendered.text,
                completion=llm_result.text,
                prompt_tokens=int(llm_result.token_usage.get("prompt_tokens", 0)),
                completion_tokens=int(llm_result.token_usage.get("completion_tokens", 0)),
            )
            return llm_result

    async def _generate_stream(
        self, llm: LLMPort, prompt: str, *, span
    ) -> LLMResult:
        """Drive the LLM in streaming mode while emitting token / reasoning
        events. Returns a synthesised `LLMResult` so the rest of the
        workflow (verification, citation extraction, response formatting)
        is unchanged."""
        text_buf: list[str] = []
        reasoning_buf: list[str] = []
        token_usage: dict[str, int] = {}
        model_id: str | None = None
        started_at = time.monotonic()
        first_token_at: float | None = None

        async for delta in llm.generate_stream(prompt):
            if delta.content:
                if first_token_at is None:
                    first_token_at = time.monotonic()
                text_buf.append(delta.content)
                await emit_token(delta.content)
            if delta.reasoning:
                reasoning_buf.append(delta.reasoning)
                await emit_reasoning(delta.reasoning)
            if delta.token_usage:
                token_usage = dict(delta.token_usage)
            if delta.model_id:
                model_id = delta.model_id

        if first_token_at is not None:
            # Captured as OTel attribute for stream-latency dashboards.
            span.set_attribute(
                "llm.first_token_ms",
                int((first_token_at - started_at) * 1000),
            )
        return LLMResult(
            text="".join(text_buf),
            token_usage=token_usage or {
                "prompt_tokens": 0,
                "completion_tokens": len("".join(text_buf)),
            },
            model_id=model_id or getattr(llm, "model_id", "unknown"),
        )

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
        resolvable_citation_ids: list[str],
        ctx: ToolExecutionContext,
        record,
        llm: LLMPort,
    ):
        retry_count = 0

        async def _run_checks(answer_text: str):
            referenced = sorted(set(_CITE_PATTERN.findall(answer_text)))
            try:
                cit = await self._tools.invoke(
                    "verification.citation_check",
                    {
                        "answer_text": answer_text,
                        "citation_ids": citation_ids,
                        "chunk_ids": chunk_ids,
                        "referenced_citation_ids": referenced,
                        "resolvable_citation_ids": resolvable_citation_ids,
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
                    oi.set_kind(s, oi.KIND_LLM)
                    oi.set_llm(
                        s,
                        model_name=llm_result.model_id,
                        prompt=rendered.text,
                        completion=llm_result.text,
                        prompt_tokens=int(llm_result.token_usage.get("prompt_tokens", 0)),
                        completion_tokens=int(llm_result.token_usage.get("completion_tokens", 0)),
                    )
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
            agent_variant=self.spec.variant_id,
            started_at=started,
            tool_calls=tuple(tool_calls),
            classification_confidence=classification_confidence,
            error_code=error_code,
        )
        await self._recorder.persist(event)
        return response


def _refusal_message(reason: RefusalReason) -> str:
    # 기획 doc §7 에러 처리 표 매핑.
    if reason is RefusalReason.CLARIFICATION_REQUIRED:
        return (
            "어떤 노형·규제에 대한 질문인지 명확히 해주세요. "
            "예: 노형명(NuScale, i-SMR), 규제 ID(RG 1.157, KINS-RG-...), RAI 번호."
        )
    if reason is RefusalReason.RETRIEVAL_NO_RESULT:
        return "관련 정보를 찾을 수 없습니다. 질의를 다른 표현으로 시도해 주세요."
    if reason is RefusalReason.VERIFICATION_FAILED:
        return "현재 자료로는 정확한 답변이 어렵습니다. 인용 가능한 근거가 부족합니다."
    if reason is RefusalReason.UNSUPPORTED_SCENARIO:
        return "현재 단계에서는 이 유형의 답변이 제한적입니다. 후속 Phase에서 지원될 예정입니다."
    if reason is RefusalReason.UNKNOWN_SCENARIO:
        return "지원되지 않는 (시나리오, 깊이) 조합입니다. 다른 형태로 질문해 주세요."
    if reason is RefusalReason.DATA_LIMITATION:
        return "자료에 명시되어 있지 않은 부분이 포함되어 있습니다. 가능한 정보만 제공합니다."
    if reason is RefusalReason.LLM_UNAVAILABLE:
        return "응답이 지연되거나 모델을 가져올 수 없습니다. 잠시 후 다시 시도해 주세요."
    if reason is RefusalReason.REFUSAL:
        return "정책상 답변을 제공할 수 없는 요청입니다."
    return "근거가 부족하여 답변을 제공할 수 없습니다."


SEQUENTIAL_TOOL_ROUTED_VARIANT_ID = "sequential_tool_routed_v2"


@register_variant(SEQUENTIAL_TOOL_ROUTED_VARIANT_ID)
def _build_sequential_tool_routed(
    spec: VariantSpec, deps: AgentDeps
) -> "SequentialToolRoutedRunner":
    t = deps.tunables
    return SequentialToolRoutedRunner(
        spec=spec,
        llm_router=deps.llm_router,
        tool_executor=deps.tool_executor,
        prompt_resolver=deps.prompt_resolver,
        prompt_renderer=deps.prompt_renderer,
        context_builder=deps.context_builder,
        recorder=deps.recorder,
        event_sink=deps.event_sink,
        app_profile=deps.app_profile,
        classifier=deps.classifier,
        classification_threshold=t.get("classification_threshold", 0.0),
        verification_citation_threshold=t.get("verification_citation_threshold", 0.5),
        verification_faithfulness_threshold=t.get("verification_faithfulness_threshold", 0.5),
        verification_retry_on_fail=t.get("verification_retry_on_fail", False),
        summarizer=deps.summarizer,
        retriever_top_k=t.get("retriever_top_k", 3),
        retriever_min_score=t.get("retriever_min_score", 0.0),
        active_cells_mode=t.get("active_cells_mode", "all"),
    )
