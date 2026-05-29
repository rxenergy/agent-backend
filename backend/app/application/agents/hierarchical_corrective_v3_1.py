from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from dataclasses import replace
from pathlib import Path
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
from app.domain.agents import Budget, VariantSpec
from app.domain.classification import ClassificationResult
from app.domain.errors import (
    PromptProfileNotFoundError,
    RefusalReason,
    VerificationStatus,
)
from app.domain.interaction import (
    AgentRequest,
    AgentResponse,
    ChatTurn,
    Citation,
    ToolCallRecord,
)
from app.application.retrieval.dispatcher import RetrievalDispatcher
from app.application.retrieval.evaluator import RetrievalEvaluator
from app.application.retrieval.planner import RetrievalPlanner
from app.domain.memory import MemoryRef, MemoryReviewStatus, StalenessStatus
from app.domain.query import QueryPlan
from app.domain.retrieval import (
    EvidencePack,
    GateDecision,
)
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMResult, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

_CITE_PATTERN = re.compile(r"\[(cite-\d+)\]")

HIERARCHICAL_CORRECTIVE_VARIANT_ID = "hierarchical_corrective_v3_1"


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class HierarchicalCorrectiveRunner:
    """v3.1 — 16-node, 4-Phase Hierarchical Corrective Workflow
    (docs/plans/hierarchical_corrective_workflow.v1.md).

    PR-2 SKELETON: the conductor wires the full 16-node frame, emits a step
    event per node, and threads the v3.1 reproducibility objects
    (`RetrievalPlan`, `EvaluationResult`, `EvidencePack`, `Budget`) into the
    `InteractionEvent`. Nodes that are not yet implemented run as explicit,
    clearly-marked **stubs** that produce valid skeleton domain objects so the
    workflow completes end-to-end:

      • Node 3  query_understanding   — stub: QueryPlan from classifier entities
      • Node 4  retrieval_plan        — stub: single-strategy plan
      • Node 5  retrieval_execute     — single `retriever.search` (no fan-out yet)
      • Node 6  retrieval_evaluate    — stub: min_score gate, all-PASS signals
      • Node 7  retrieval_recover     — stub: skipped (PR-9)
      • Node 8  multi_hop_expand      — stub: skipped (PR-9)
      • Node 9  evidence_snippet      — stub: empty pack, chunks used directly (PR-6)
      • Node 14 claim_decompose       — stub: no claims (PR-8)
      • Node 15 claim_verify          — citation + faithfulness gate (reused), no per-claim (PR-8)
      • Node 16 selective_regenerate  — stub: skipped (PR-8)

    Real (reused) nodes: 1 classification, 2 routing, 10 memory, 11 context,
    12 prompt render (+ citation contract preamble), 13 generation.

    Each subsequent PR replaces a stub body in place without changing the
    conductor's call sites — the AgentRunner contract (`run` / `run_stream`)
    stays fixed (CLAUDE.md principle 1)."""

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
        summarizer: ConversationSummarizer | None = None,
        retriever_top_k: int = 3,
        retriever_min_score: float = 0.0,
        retrieval_fetch_k: int = 20,
        active_cells_mode: str = "all",
        llm_call_budget: int = 8,
        citation_contract_path: str | None = None,
        retrieval_planner: RetrievalPlanner | None = None,
        retrieval_evaluator: RetrievalEvaluator | None = None,
        regulatory_hard_gates_enforced: bool = False,
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
        self._summarizer = summarizer
        self._top_k = retriever_top_k
        self._min_score = retriever_min_score
        self._fetch_k = max(retrieval_fetch_k, retriever_top_k)
        self._active_cells_mode = active_cells_mode
        self._llm_call_budget = llm_call_budget
        # Node 4/5 — 룰 기반 planner + 다전략 RRF dispatcher. planner 미주입 시
        # 단일 hybrid 폴백. dispatcher 는 tool_executor 위의 얇은 래퍼.
        self._planner = retrieval_planner or RetrievalPlanner.default()
        self._dispatcher = RetrievalDispatcher(tool_executor, rrf_k=self._planner.rrf_k)
        # Node 6 — 5-신호 evaluator. regulatory hard gate 강제 여부는
        # opensearch_schema_version=="v2" 에 연동(profiles 에서 주입).
        self._evaluator = retrieval_evaluator or RetrievalEvaluator.default()
        self._regulatory_enforced = regulatory_hard_gates_enforced
        # Node 12 — citation contract preamble (PR-7 fragment). Loaded once;
        # prepended to the context block so it rides in the rendered prompt and
        # its presence is reflected in `rendered_prompt_hash`.
        self._citation_contract: str | None = None
        self._citation_contract_sha: str | None = None
        if citation_contract_path:
            p = Path(citation_contract_path)
            if p.is_file():
                self._citation_contract = p.read_text(encoding="utf-8")
                self._citation_contract_sha = _sha16(self._citation_contract)

    # ------------------------------------------------------------------
    # Streaming wrapper — identical pattern to v2 (proven).
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
    # 16-node conductor.
    # ------------------------------------------------------------------
    async def run(self, request: AgentRequest) -> AgentResponse:
        started = time.monotonic()
        tool_calls: list[ToolCallRecord] = []
        tool_result_refs: list[str] = []
        # Live LLM-call budget (Node 13/14/16 increment). Snapshotted into the
        # event at the end. PR-2 only Node 13 spends.
        llm_calls_used = 0
        budget_hit: list[str] = []

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

            # === Phase A ===================================================
            # Node 1 — intent_classification
            await emit_step("intent_classification", "started")
            with _TRACER.start_as_current_span("agent.intent_classification") as s:
                classification = await self._classify(request)
                scenario_object = classification.scenario_object
                scenario_depth = classification.scenario_depth
                entities = classification.entities
                conf = classification.confidence
                s.set_attribute("scenario_object", scenario_object)
                s.set_attribute("scenario_depth", scenario_depth)
                s.set_attribute("classification_confidence", conf)
                oi.set_kind(s, oi.KIND_CHAIN)
            await emit_step("intent_classification", "ok",
                            scenario_object=scenario_object,
                            scenario_depth=scenario_depth, confidence=conf)

            # Node 2 — scenario_routing
            await emit_step("scenario_routing", "started")
            if self._classifier is not None and conf < self._classification_threshold:
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.CLARIFICATION_REQUIRED, conf,
                    verification_status=VerificationStatus.SKIPPED,
                    error_code="classification_low_confidence",
                )
            if not is_active(scenario_object, scenario_depth, mode=self._active_cells_mode):
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.UNSUPPORTED_SCENARIO, conf,
                    verification_status=VerificationStatus.SKIPPED,
                    error_code="inactive_cell",
                )
            ctx = replace(ctx, scenario_object=scenario_object, scenario_depth=scenario_depth)
            await emit_step("scenario_routing", "ok")

            # Node 3 — query_understanding (STUB: no NER/normalize/sub-Q yet)
            await emit_step("query_understanding", "started")
            query_plan = QueryPlan(
                normalized_entities=entities,
                multi_intent=False,
            )
            await emit_step("query_understanding", "ok",
                            multi_intent=query_plan.multi_intent,
                            sub_questions=len(query_plan.sub_questions))

            # === Phase B ===================================================
            # Node 4 — retrieval_plan_template (룰 기반, LLM 미사용)
            await emit_step("retrieval_plan", "started")
            plan = self._planner.plan(
                scenario_object=scenario_object,
                scenario_depth=scenario_depth,
                entities=entities,
                intents=query_plan.intents,
            )
            await emit_step("retrieval_plan", "ok", rule_id=plan.rule_id,
                            plan_hash=plan.plan_hash,
                            strategies=[s.name for s in plan.strategies])

            # Node 3 pre-step memory.session_load (needed by Node 10 decision).
            session_load = await self._tools.invoke(
                "memory.session_load", {"session_id": request.session_id}, ctx,
            )
            record(session_load)
            prior_so = prior_sd = None
            prior_entities: dict[str, list[str]] = {}
            conversation_summary: str | None = None
            if session_load.output and session_load.output.get("present"):
                prior_so = session_load.output.get("active_scenario_object")
                prior_sd = session_load.output.get("active_scenario_depth")
                prior_entities = session_load.output.get("active_entities") or {}
                conversation_summary = session_load.output.get("conversation_summary")

            # Node 5 — retrieval_execute (다전략 fan-out + RRF)
            await emit_step("retrieval_execute", "started",
                            strategies=[st.name for st in plan.strategies])
            try:
                dispatch = await self._dispatcher.execute(
                    plan,
                    query_text=request.query_text,
                    fetch_k=self._fetch_k,
                    scenario_object=scenario_object,
                    scenario_depth=scenario_depth,
                    entities=entities,
                    ctx=ctx,
                    min_score=self._min_score,  # raw 필터는 dispatcher 가 융합 전 적용
                )
            except RequiredToolFailed as e:
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.RETRIEVAL_NO_RESULT, conf, error_code=e.code.value,
                )
            for r in dispatch.tool_results:
                record(r)
            # 실제 실행된 전략으로 plan 을 확정(감사 trace 에 전략명 명시).
            if dispatch.executed:
                plan = replace(plan, strategies=tuple(dispatch.executed))
            # 융합 순서가 권위(RRF rank). raw score 로 재필터하지 않는다(계약).
            # 깊은 fetch 풀에서 상위 top_k 만 다운스트림으로.
            pool = dispatch.fused_chunks
            chunks = pool[: self._top_k]
            if not chunks:
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.RETRIEVAL_NO_RESULT, conf, error_code="tool_empty_result",
                )
            await emit_step("retrieval_execute", "ok",
                            num_chunks=len(chunks), pool_size=len(pool),
                            strategies_ok=[s.name for s in dispatch.executed],
                            strategies_failed=dispatch.failed_strategies)

            # Node 6 — retrieval_evaluate (5-신호 결정론 게이트)
            await emit_step("retrieval_evaluate", "started")
            evaluation = self._evaluator.evaluate(
                chunks,
                query_text=request.query_text,
                entities=entities,
                version_constraint=query_plan.version_constraint,
                rrf_scores=dispatch.rrf_scores,
                regulatory_enforced=self._regulatory_enforced,
            )
            # NOTE: verdict 는 PR-5 에선 *기록·표면화*까지. WEAK→recover / FAIL
            # exhausted→refuse 분기는 Node 7(recover, PR-9)에서 배선된다. 그
            # 전까지 워크플로우는 verdict 와 무관하게 진행하되, regulatory_enforced
            # 와 per_chunk_signals 가 event 에 실려 사후 판단을 가능케 한다.
            await emit_step("retrieval_evaluate", "ok",
                            overall=evaluation.overall_decision,
                            regulatory_enforced=evaluation.regulatory_enforced,
                            num_pass=sum(1 for s in evaluation.per_chunk
                                         if s.decision == GateDecision.PASS.value))

            # Node 7 — retrieval_recover (STUB: skipped; PR-9)
            await emit_step("retrieval_recover", "skipped")
            # Node 8 — multi_hop_expand (STUB: skipped; PR-9)
            await emit_step("multi_hop_expand", "skipped")

            # Node 9 — evidence_snippet (STUB: empty pack; chunks fed directly; PR-6)
            await emit_step("evidence_snippet", "started")
            evidence_pack = EvidencePack(snippets=(), pack_hash=None,
                                         snippet_extractor_version=None)
            await emit_step("evidence_snippet", "ok", num_snippets=0)

            # === Phase C ===================================================
            # Node 5 pre-step approved memory search + Node 10 decision.
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

            decision = decide_session_injection(
                has_chat_history=bool(request.chat_history),
                prior_scenario_object=prior_so,
                prior_scenario_depth=prior_sd,
                current_scenario_object=scenario_object,
                current_scenario_depth=scenario_depth,
                prior_entities=prior_entities,
                current_entities=entities,
            )

            # Node 10 — memory_inject
            await emit_step("memory_inject", "started")
            memory_refs: tuple[MemoryRef, ...] = ()
            memory_ids_used: list[str] = []
            memory_types_used: list[str] = []
            memory_review_statuses: dict[str, str] = {}
            memory_staleness_statuses: dict[str, str] = {}
            memory_retrieval_scores: dict[str, float] = {}
            if decision.inject and session_load.output and session_load.output.get("present"):
                sid = request.session_id or ""
                memory_ids_used.append(sid)
                memory_types_used.append("session")
                memory_review_statuses[sid] = MemoryReviewStatus.APPROVED.value
                memory_staleness_statuses[sid] = StalenessStatus.FRESH.value
                memory_refs = (
                    MemoryRef(
                        memory_id=sid, memory_type="session",
                        review_status=MemoryReviewStatus.APPROVED.value,
                        staleness_status=StalenessStatus.FRESH.value,
                    ),
                )
            for hit in (approved.output or {}).get("hits", []) or []:
                mid = hit.get("memory_id")
                if not mid:
                    continue
                memory_retrieval_scores[mid] = float(hit.get("score", 0.0))
                memory_ids_used.append(mid)
                memory_types_used.append("approved")
                memory_review_statuses[mid] = MemoryReviewStatus.APPROVED.value
                memory_staleness_statuses[mid] = StalenessStatus.FRESH.value
                memory_refs = memory_refs + (
                    MemoryRef(
                        memory_id=mid, memory_type="approved",
                        review_status=MemoryReviewStatus.APPROVED.value,
                        staleness_status=StalenessStatus.FRESH.value,
                    ),
                )
            if self._summarizer is not None:
                summ = await self._summarizer.summarize(
                    prior_summary=conversation_summary,
                    chat_history=request.chat_history,
                )
                conversation_summary = summ.summary or conversation_summary
            await emit_step("memory_inject", "ok", inject=decision.inject,
                            num_memory_refs=len(memory_refs))

            # Node 11 — context_build
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
            await emit_step("context_build", "ok", context_hash=pack.context_hash)

            # Node 12 — prompt_render (+ citation contract preamble)
            await emit_step("prompt_render", "started")
            with _TRACER.start_as_current_span("agent.prompt_render") as s:
                try:
                    profile = self._resolver.resolve(scenario_object, scenario_depth)
                except PromptProfileNotFoundError:
                    return await self._refuse(
                        request, started, tool_calls, scenario_object, scenario_depth,
                        RefusalReason.UNKNOWN_SCENARIO, conf,
                        verification_status=VerificationStatus.SKIPPED,
                        error_code="prompt_profile_not_found",
                    )
                context_block = self._context_builder.render_for_prompt(pack)
                if self._citation_contract:
                    context_block = (
                        "# CITATION CONTRACT\n"
                        + self._citation_contract.strip()
                        + "\n\n"
                        + context_block
                    )
                rendered = self._renderer.render(
                    profile, query_text=request.query_text, context_block=context_block,
                )
                s.set_attribute("rendered_prompt_hash", rendered.rendered_prompt_hash)
                if self._citation_contract_sha:
                    s.set_attribute("citation_contract_sha", self._citation_contract_sha)
                oi.set_kind(s, oi.KIND_CHAIN)
                await self._sink.write_prompt_render_record(
                    request.interaction_id,
                    self._renderer.to_record(rendered, query_text=request.query_text),
                )
                await self._sink.write_context_snapshot(
                    request.interaction_id, self._context_builder.to_snapshot(pack),
                )
            await emit_step("prompt_render", "ok", profile_id=rendered.profile_id,
                            profile_version=rendered.profile_version)

            # Node 13 — generation
            await emit_step("generation", "started", llm_id=llm_id)
            llm_result = await self._generate(
                request, rendered, started, tool_calls,
                scenario_object, scenario_depth, conf, llm=llm,
            )
            if isinstance(llm_result, AgentResponse):
                return llm_result  # LLM-unavailable refusal
            llm_calls_used += 1
            await emit_step("generation", "ok",
                            completion_tokens=llm_result.token_usage.get("completion_tokens", 0))

            citation_ids = [c.citation_id for c in pack.citation_candidates]
            chunk_ids = [c.chunk_id for c in chunks]

            # document.resolve_citation overlay (feeds verification + response).
            try:
                resolve = await self._tools.invoke(
                    "document.resolve_citation",
                    {"citation_ids": citation_ids, "chunk_ids": chunk_ids}, ctx,
                )
            except RequiredToolFailed as e:
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.VERIFICATION_FAILED, conf, error_code=e.code.value,
                )
            record(resolve)
            resolved_by_cid: dict[str, dict[str, Any]] = {
                r.get("citation_id"): r
                for r in (resolve.output or {}).get("resolved", []) or []
                if r.get("citation_id")
            }
            final_candidates = tuple(
                replace(
                    c,
                    document_id=(resolved_by_cid.get(c.citation_id) or {}).get("document_id") or c.document_id,
                    page=(resolved_by_cid.get(c.citation_id) or {}).get("page") or c.page,
                    section=(resolved_by_cid.get(c.citation_id) or {}).get("section") or c.section,
                    revision=(resolved_by_cid.get(c.citation_id) or {}).get("revision") or c.revision,
                )
                for c in pack.citation_candidates
            )
            resolvable_citation_ids = [
                cid for cid, r in resolved_by_cid.items() if r.get("resolvable", False)
            ]

            # === Phase D ===================================================
            # Node 14 — claim_decompose (STUB: no claims yet; PR-8)
            await emit_step("claim_decompose", "skipped")
            claims: tuple = ()

            # Node 15 — claim_verify (citation + faithfulness gate reused; the
            # per-claim 4-step circuit lands in PR-8).
            await emit_step("claim_verify", "started")
            citation_completeness, faithfulness = await self._run_checks(
                llm_result.text, citation_ids, chunk_ids, resolvable_citation_ids, ctx, record,
            )
            ok = (citation_completeness >= self._cit_thr
                  and faithfulness >= self._faith_thr)
            if ok:
                verification_status = VerificationStatus.PASS.value
            elif (citation_completeness >= self._cit_thr * 0.5
                  and faithfulness >= self._faith_thr * 0.5):
                verification_status = VerificationStatus.PARTIAL.value
            else:
                verification_status = VerificationStatus.FAIL.value
            await emit_step("claim_verify", "ok",
                            verification_status=verification_status,
                            citation_completeness=citation_completeness,
                            faithfulness=faithfulness)

            # Node 16 — selective_regenerate (STUB: skipped; PR-8)
            await emit_step("selective_regenerate", "skipped")

            # session_update
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

            # === response_format ==========================================
            budget = Budget(
                llm_calls_used=llm_calls_used,
                total_llm_call_budget=self._llm_call_budget,
                budget_hit=tuple(budget_hit),
            )
            with _TRACER.start_as_current_span("agent.response_format") as _rfmt:
                if verification_status == VerificationStatus.FAIL.value:
                    refusal = RefusalReason.VERIFICATION_FAILED.value
                    answer_text = _refusal_message(RefusalReason.VERIFICATION_FAILED)
                    citations: tuple[Citation, ...] = ()
                elif verification_status == VerificationStatus.PARTIAL.value:
                    refusal = RefusalReason.PARTIAL_ANSWER.value
                    answer_text = (
                        llm_result.text
                        + "\n\n[부분 답변] 일부 인용·근거가 임계값을 충족하지 못했습니다."
                    )
                    citations = _to_citations(final_candidates)
                else:
                    refusal = None
                    answer_text = llm_result.text
                    citations = _to_citations(final_candidates)
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
                    classification_confidence=conf,
                    classifier_backend=classification.classifier_backend,
                    entities=entities,
                    llm_id=llm_id,
                    model_id=llm_result.model_id,
                    claims=claims,
                    evaluation=evaluation,
                    recover_rounds=(),
                    hops=(),
                )
                oi.set_kind(_rfmt, oi.KIND_CHAIN)

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
                    classification_confidence=conf,
                    citation_completeness=citation_completeness,
                    faithfulness=faithfulness,
                    started_at=started,
                    tool_calls=tuple(tool_calls),
                    memory_ids_used=tuple(memory_ids_used),
                    memory_types_used=tuple(memory_types_used),
                    memory_review_statuses=memory_review_statuses,
                    memory_staleness_statuses=memory_staleness_statuses,
                    memory_retrieval_scores=memory_retrieval_scores,
                    # v3.1 reproducibility
                    query_understanding={
                        "multi_intent": query_plan.multi_intent,
                        "sub_question_count": len(query_plan.sub_questions),
                        "ner_dict_version": query_plan.ner_dict_version,
                        "normalizer_version": query_plan.normalizer_version,
                        "decompose_prompt_hash": query_plan.decompose_prompt_hash,
                        # Node 12 citation-contract version. Recorded here (not
                        # in fragment_versions) because the contract is wired as
                        # a context-block preamble, not a registered prompt
                        # fragment (PR-2 deviation from plan §2.4). Lets the
                        # event alone attribute which contract shaped the prompt.
                        "citation_contract_sha": self._citation_contract_sha,
                    },
                    retrieval_plan_hash=plan.plan_hash,
                    evaluator_policy_hash=evaluation.evaluator_policy_hash,
                    regulatory_enforced=evaluation.regulatory_enforced,
                    per_chunk_signals=evaluation.per_chunk,
                    per_sub_question_decisions=evaluation.per_sub_question,
                    recover_rounds=(),
                    hops=(),
                    evidence_pack_hash=evidence_pack.pack_hash,
                    claims=claims,
                    verifier_policy_hash=None,
                    entailment_model=None,
                    budget=budget,
                )
                await self._recorder.persist(event)
                s.set_attribute("interaction_id", request.interaction_id)

            root.set_attribute("verification_status", verification_status)
            root.set_attribute("latency_ms", response.latency_ms)
            oi.set_io(root, output_value=response.answer_text)

        return response

    # ------------------------------------------------------------------
    async def _classify(self, request: AgentRequest) -> ClassificationResult:
        # Reuse the v2 Node 1 shim (ADR-0003).
        from app.application.agents.sequential.nodes.classify import classify
        return await classify(request, self._classifier)

    async def _run_checks(self, answer_text, citation_ids, chunk_ids,
                          resolvable_citation_ids, ctx, record):
        referenced = sorted(set(_CITE_PATTERN.findall(answer_text)))
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
        record(cit)
        cc = float((cit.output or {}).get("citation_completeness", 0.0))
        f = await self._tools.invoke(
            "verification.faithfulness_check",
            {"answer_text": answer_text, "chunk_ids": chunk_ids}, ctx,
        )
        record(f)
        fh = float((f.output or {}).get("faithfulness", 0.0))
        return cc, fh

    async def _generate(self, request, rendered: RenderedPrompt, started, tool_calls,
                        scenario_object, scenario_depth, conf, *, llm: LLMPort):
        with _TRACER.start_as_current_span("llm.generation") as s:
            em = current_emitter()
            try:
                if em.active:
                    llm_result = await self._generate_stream(llm, rendered.text, span=s)
                else:
                    llm_result = await llm.generate(rendered.text)
            except LLMUnavailableError as exc:
                s.set_attribute("llm.status", "unavailable")
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.LLM_UNAVAILABLE, conf, error_code="llm_unavailable",
                )
            s.set_attribute("model_id", llm_result.model_id)
            oi.set_kind(s, oi.KIND_LLM)
            oi.set_llm(
                s, model_name=llm_result.model_id, prompt=rendered.text,
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

    async def _refuse(self, request, started, tool_calls, scenario_object,
                      scenario_depth, reason: RefusalReason, conf, *,
                      error_code: str | None,
                      verification_status: VerificationStatus = VerificationStatus.FAIL):
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
            request=request, response=response, agent_variant=self.spec.variant_id,
            started_at=started, tool_calls=tuple(tool_calls),
            classification_confidence=conf, error_code=error_code,
        )
        await self._recorder.persist(event)
        return response


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
    if reason is RefusalReason.RETRIEVAL_NO_RESULT:
        return "관련 정보를 찾을 수 없습니다. 질의를 다른 표현으로 시도해 주세요."
    if reason is RefusalReason.VERIFICATION_FAILED:
        return "현재 자료로는 정확한 답변이 어렵습니다. 인용 가능한 근거가 부족합니다."
    if reason is RefusalReason.INSUFFICIENT_EVIDENCE:
        return "검색 복구를 시도했으나 답변 자격을 충족하는 근거를 확보하지 못했습니다."
    if reason is RefusalReason.BUDGET_EXCEEDED:
        return "처리 예산을 초과하여 답변을 완료하지 못했습니다. 질의를 좁혀 다시 시도해 주세요."
    if reason is RefusalReason.UNSUPPORTED_SCENARIO:
        return "현재 단계에서는 이 유형의 답변이 제한적입니다. 후속 Phase에서 지원될 예정입니다."
    if reason is RefusalReason.UNKNOWN_SCENARIO:
        return "지원되지 않는 (시나리오, 깊이) 조합입니다. 다른 형태로 질문해 주세요."
    if reason is RefusalReason.LLM_UNAVAILABLE:
        return "응답이 지연되거나 모델을 가져올 수 없습니다. 잠시 후 다시 시도해 주세요."
    return "근거가 부족하여 답변을 제공할 수 없습니다."


@register_variant(HIERARCHICAL_CORRECTIVE_VARIANT_ID)
def _build_hierarchical_corrective(
    spec: VariantSpec, deps: AgentDeps
) -> "HierarchicalCorrectiveRunner":
    t = deps.tunables
    return HierarchicalCorrectiveRunner(
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
        summarizer=deps.summarizer,
        retriever_top_k=t.get("retriever_top_k", 3),
        retriever_min_score=t.get("retriever_min_score", 0.0),
        retrieval_fetch_k=t.get("retrieval_fetch_k", 20),
        active_cells_mode=t.get("active_cells_mode", "all"),
        llm_call_budget=t.get("llm_call_budget", 8),
        citation_contract_path=t.get("citation_contract_path"),
        retrieval_planner=deps.retrieval_planner,
        retrieval_evaluator=deps.retrieval_evaluator,
        regulatory_hard_gates_enforced=t.get("regulatory_hard_gates_enforced", False),
    )
