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
from app.application.retrieval.corpus_map import CorpusMap, ScopeDecision
from app.application.retrieval.dispatcher import RetrievalDispatcher
from app.application.retrieval.evaluator import RetrievalEvaluator
from app.application.retrieval.planner import RetrievalPlanner
from app.application.retrieval.recovery import RetrievalRecoverer
from app.application.retrieval.snippet import SnippetExtractor
from app.application.verification.claim_decompose import ClaimDecomposer
from app.application.verification.claim_verifier import ClaimVerifier
from app.application.verification.entailment import EntailmentChecker
from app.domain.verification import ClaimStatus
from app.domain.memory import MemoryRef, MemoryReviewStatus, StalenessStatus
from app.domain.query import QueryPlan
from app.domain.retrieval import (
    GateDecision,
    HopEdge,
    RecoverRound,
    RetrieverSearchOutput,
)
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMResult, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

HIERARCHICAL_CORRECTIVE_VARIANT_ID = "hierarchical_corrective_v3_1"


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# Node 8 다홉 — 같은-문서 섹션 cross-reference 결정론 추출(LLM 미사용).
# "§3.2", "Section 3.5.1.3" → 섹션 번호. 외부문서(RG 1.X)·clause_id 는 v1 에서
# 추적 불가(clause_id null)라 여기서 잡지 않는다(v2-gated).
# ≥1 dot 요구 — 맨숫자("section 3")는 prefix 가 한 장 전체(3.x)를 끌어와 노이즈가
# 크므로 다단계 번호(3.2, 3.5.1.3)만 hop 대상으로 본다.
_SECTION_REF_RE = re.compile(r"(?:§|[Ss]ection\s+)(\d+(?:\.\d+){1,3})")
# 다홉 fetch 호출 상한(폭주·예산 방지).
_MAX_HOPS = 8


def _chunk_ordinal(chunk_id: str) -> int:
    """`..._cNNNN` 의 trailing int(섹션 내 문단 순서). zero-pad 가 9999 를 넘으면
    사전식 정렬이 깨지므로 정수로 정렬. 못 찾으면 0."""
    m = re.search(r"_c(\d+)$", chunk_id or "")
    return int(m.group(1)) if m else 0


# Node 6/7 진단 라벨(RetrievalRecoverer.diagnose) → 생성 LLM 이 사용자에게 설명할
# 수 있는 한국어 사유. WEAK advisory(_retrieval_quality_note)에서 재사용.
_RETRIEVAL_DIAGNOSIS_REASON = {
    "entity_coverage_low": "질의의 핵심 용어·엔티티(노형명·규제 ID 등)와 검색된 근거의 매칭이 약합니다.",
    "low_scores": "검색된 근거의 어휘·의미 일치 점수가 전반적으로 낮습니다.",
    "generic": "통과 기준을 명확히 충족한 근거가 부족합니다(다수 근거가 경계선 점수).",
    "no_results": "충분한 근거를 확보하지 못했습니다.",
}


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
        utility_llm: LLMPort | None = None,
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
        claim_verification_enabled: bool = True,
        summarizer: ConversationSummarizer | None = None,
        retriever_top_k: int = 3,
        retriever_min_score: float = 0.0,
        retrieval_fetch_k: int = 20,
        active_cells_mode: str = "all",
        llm_call_budget: int = 8,
        citation_contract_path: str | None = None,
        retrieval_planner: RetrievalPlanner | None = None,
        retrieval_evaluator: RetrievalEvaluator | None = None,
        retrieval_recoverer: RetrievalRecoverer | None = None,
        regulatory_hard_gates_enforced: bool = False,
        corpus_map: CorpusMap | None = None,
        scope_tau_high: float = 0.6,
        scope_tau_low: float = 0.3,
        scope_min_token_count: int = 0,
        section_merge_max_chunks: int = 50,
        context_token_budget: int = 0,
    ) -> None:
        self.spec = spec
        self._llm_router = llm_router
        self._utility_llm = utility_llm  # Node 14/15 LLM(분해·함의). None 이면 생성 LLM 사용.
        self._tools = tool_executor
        self._resolver = prompt_resolver
        self._renderer = prompt_renderer
        # Node 9 가 문장 window 를 prompt evidence 로 싣는 것이 v3.1 설계 전제다.
        # 주입된 builder 의 전역 capture_mode(프로덕션 기본 "metadata")에 의존하면
        # render_for_prompt 가 window 를 "(metadata-only capture)" 로 버린다 →
        # Node 9 가 무력화. v3.1 은 항상 snippets 모드로 렌더해 window(chunk.snippet
        # 에 주입됨)가 prompt 에 확실히 닿게 한다. (text 분기는 full 모드에서만
        # 발동하므로 snippets 모드에선 항상 snippet=window 사용.)
        self._context_builder = ContextBuilder(capture_mode="snippets")
        self._recorder = recorder
        self._sink = event_sink
        self._app_profile = app_profile
        self._classifier = classifier
        self._classification_threshold = classification_threshold
        self._cit_thr = verification_citation_threshold
        self._faith_thr = verification_faithfulness_threshold
        # Phase D(Node 14/15) claim 검증 토글. False 면 사후 검증을 통째로 건너뛰고
        # verification_status=SKIPPED 로 답변을 그대로 통과시킨다(생성-검증 결합
        # 결함의 임시 우회 — streaming 에선 이미 전송된 텍스트라 되돌릴 수 없음).
        self._claim_verification_enabled = claim_verification_enabled
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
        # Node 7 — 결정론 recover(동의어 확장 / filter 완화, max 2 round).
        self._recoverer = retrieval_recoverer or RetrievalRecoverer.default()
        # Node 9 — 문장 window 추출기(결정론 정규식 splitter 기본).
        self._snippet_extractor = SnippetExtractor()
        # Layer 1 범위 한정(corpus_map) — confidence-게이트 scope. 미주입 시 빈 맵
        # (scope off, noise floor 0). tau_high/low 가 filter/boost/off 분기를 정함.
        self._corpus_map = corpus_map or CorpusMap.default()
        self._scope_tau_high = scope_tau_high
        self._scope_tau_low = scope_tau_low
        self._scope_min_token_count = scope_min_token_count
        # P1 Section auto-merge(수직) + P1b 예산 거버너. 거버너는 budget>0 일 때만
        # 활성(기본 0 → drop/demote/재배치 없음 → 기존 동작 보존).
        self._section_merge_max_chunks = section_merge_max_chunks
        self._context_token_budget = context_token_budget
        # 병합 정책 재현 단위 — 섹션 깊이(최말단)·상한·승격 규칙을 핀.
        self._section_merge_policy_hash = _sha16(
            f"deepest|max={section_merge_max_chunks}|promote=non_pass"
        )
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
                oi.set_io(
                    s,
                    input_value=request.query_text,
                    output_value={
                        "scenario_object": scenario_object,
                        "scenario_depth": scenario_depth,
                        "confidence": conf,
                        "entities": entities,
                    },
                )
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
            # Layer 1 범위 한정 — corpus_map 이 (scenario_object/entities/intents)와
            # 분류 confidence 로 scope 를 해석. high→filter / mid→boost / low→off.
            # 잘못된 hard scope 의 recall 절벽을 confidence 게이트로 방어한다.
            scope = self._corpus_map.resolve_scope(
                scenario_object=scenario_object,
                scenario_depth=scenario_depth,
                intents=query_plan.intents,
                entities=entities,
                confidence=conf,
                tau_high=self._scope_tau_high,
                tau_low=self._scope_tau_low,
                settings_min_token_count=self._scope_min_token_count,
            )
            await emit_step("retrieval_plan", "ok", rule_id=plan.rule_id,
                            plan_hash=plan.plan_hash,
                            strategies=[s.name for s in plan.strategies],
                            scope_mode=scope.mode,
                            scope_min_token_count=scope.min_token_count)

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

            # Node 5 — retrieval_execute (다전략 fan-out + RRF). span 이 전략별
            # tool.retriever.search 를 묶어 Phoenix 에서 한 그룹으로 보이게 한다.
            await emit_step("retrieval_execute", "started",
                            strategies=[st.name for st in plan.strategies])
            with _TRACER.start_as_current_span("agent.retrieval_execute") as s:
                oi.set_kind(s, oi.KIND_RETRIEVER)
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
                        # Layer 1/2 — 첫 검색엔 scope 전부(boost/filter) + noise floor.
                        target=scope.target,
                        filters=scope.filters,
                        min_token_count=scope.min_token_count,
                    )
                except RequiredToolFailed as e:
                    return await self._refuse(
                        request, started, tool_calls, scenario_object, scenario_depth,
                        RefusalReason.RETRIEVAL_NO_RESULT, conf, error_code=e.code.value,
                        scope=scope,
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
                        scope=scope,
                    )
                s.set_attribute("retrieval.num_chunks", len(chunks))
                s.set_attribute("retrieval.pool_size", len(pool))
                s.set_attribute("retrieval.strategies_ok",
                                [st.name for st in dispatch.executed])
                if dispatch.failed_strategies:
                    s.set_attribute("retrieval.strategies_failed",
                                    dispatch.failed_strategies)
            await emit_step("retrieval_execute", "ok",
                            num_chunks=len(chunks), pool_size=len(pool),
                            strategies_ok=[s.name for s in dispatch.executed],
                            strategies_failed=dispatch.failed_strategies,
                            # 표시용 — thinking 요약이 "어떤 문서를 근거로 쓰는지"를
                            # 보여줄 수 있게 상위 chunk 메타를 싣는다(로직 불변, 감사
                            # 상세는 사이드채널/span 이 별도 보유).
                            chunks_preview=[
                                {
                                    "title": c.title,
                                    "document_id": c.document_id,
                                    "section": c.section,
                                    "page": c.page,
                                    "doc_type": c.doc_type,
                                    "score": c.score,
                                }
                                for c in chunks
                            ])

            # Node 6 — retrieval_evaluate (5-신호 결정론 게이트)
            await emit_step("retrieval_evaluate", "started")
            with _TRACER.start_as_current_span("agent.retrieval_evaluate") as s:
                oi.set_kind(s, oi.KIND_CHAIN)
                evaluation = self._evaluator.evaluate(
                    chunks,
                    query_text=request.query_text,
                    entities=entities,
                    version_constraint=query_plan.version_constraint,
                    rrf_scores=dispatch.rrf_scores,
                    regulatory_enforced=self._regulatory_enforced,
                )
                num_pass = sum(1 for sig in evaluation.per_chunk
                               if sig.decision == GateDecision.PASS.value)
                s.set_attribute("evaluate.overall_decision", evaluation.overall_decision)
                s.set_attribute("evaluate.regulatory_enforced",
                                evaluation.regulatory_enforced)
                if evaluation.evaluator_policy_hash:
                    s.set_attribute("evaluate.policy_hash",
                                    evaluation.evaluator_policy_hash)
                s.set_attribute("evaluate.num_pass", num_pass)
                s.set_attribute("evaluate.num_chunks", len(evaluation.per_chunk))
                oi.set_io(s, output_value={
                    "overall_decision": evaluation.overall_decision,
                    "num_pass": num_pass,
                })
            await emit_step("retrieval_evaluate", "ok",
                            overall=evaluation.overall_decision,
                            regulatory_enforced=evaluation.regulatory_enforced,
                            num_pass=num_pass,
                            # PASS 가 아닐 때만 사용자에게 "근거가 왜 부분적인지"를
                            # 설명할 한국어 사유를 싣는다(요약 thinking 전용).
                            diagnosis_reason=(
                                _RETRIEVAL_DIAGNOSIS_REASON.get(
                                    self._recoverer.diagnose(evaluation))
                                if evaluation.overall_decision != GateDecision.PASS.value
                                else None
                            ))

            # Node 7 — retrieval_recover. WEAK/FAIL → 결정론 진단·복구 → Node 5
            # 재-dispatch → Node 6 재평가. max N round. 경계는 dispatch→evaluate 만
            # 감싸고, 복구된 chunks/evaluation 이 downstream(Node 9~)으로 흐른다.
            recover_rounds: list[RecoverRound] = []
            cur_fetch_k = self._fetch_k
            cur_min_score = self._min_score
            cur_entities = entities
            rnd = 0
            while (
                evaluation.overall_decision != GateDecision.PASS.value
                and rnd < self._recoverer.max_rounds
            ):
                diagnosis = self._recoverer.diagnose(evaluation)
                action = self._recoverer.plan_action(
                    diagnosis, entities=cur_entities,
                    fetch_k=cur_fetch_k, min_score=cur_min_score,
                )
                await emit_step("retrieval_recover", "started",
                                round=rnd, diagnosis=diagnosis,
                                strategy=action.strategy_id)
                cur_entities = action.entities
                cur_fetch_k = action.fetch_k
                cur_min_score = action.min_score
                # 라운드별 span — 재-dispatch 의 tool.retriever.search 가 이 span
                # 아래 nest 되어 Phoenix 에서 어느 복구 라운드인지 구분된다.
                with _TRACER.start_as_current_span("agent.retrieval_recover") as rs:
                    oi.set_kind(rs, oi.KIND_RETRIEVER)
                    rs.set_attribute("recover.round", rnd)
                    rs.set_attribute("recover.diagnosis", diagnosis)
                    rs.set_attribute("recover.strategy", action.strategy_id)
                    rs.set_attribute("recover.fetch_k", cur_fetch_k)
                    rs.set_attribute("recover.min_score", cur_min_score)
                    rs.set_attribute("recover.scope_filters_dropped", bool(scope.filters))
                    try:
                        # 복구는 *모집단을 넓히는* 행위 — high-confidence hard scope 가
                        # 정답을 배제했을 1순위 의심(recall 절벽)이므로 filters 를 해제한다.
                        # boost(target)는 recall-safe 라 유지, noise floor 는 품질이라 유지.
                        dispatch = await self._dispatcher.execute(
                            plan, query_text=request.query_text, fetch_k=cur_fetch_k,
                            scenario_object=scenario_object, scenario_depth=scenario_depth,
                            entities=cur_entities, ctx=ctx, min_score=cur_min_score,
                            target=scope.target,
                            filters={},
                            min_token_count=scope.min_token_count,
                        )
                    except RequiredToolFailed:
                        rs.set_attribute("recover.aborted", True)
                        break
                    for r in dispatch.tool_results:
                        record(r)  # 복구 라운드의 재검색도 tool_calls 에 기록(재현성).
                    pool = dispatch.fused_chunks
                    chunks = pool[: self._top_k]
                    evaluation = self._evaluator.evaluate(
                        chunks, query_text=request.query_text, entities=cur_entities,
                        version_constraint=query_plan.version_constraint,
                        rrf_scores=dispatch.rrf_scores,
                        regulatory_enforced=self._regulatory_enforced,
                    )
                    rs.set_attribute("recover.outcome", evaluation.overall_decision)
                recover_rounds.append(
                    RecoverRound(
                        round_index=rnd, diagnosis=diagnosis,
                        recover_strategy_id=action.strategy_id,
                        outcome_decision=evaluation.overall_decision,
                    )
                )
                await emit_step("retrieval_recover", "ok", round=rnd,
                                outcome=evaluation.overall_decision)
                rnd += 1
            if not recover_rounds:
                await emit_step("retrieval_recover", "skipped")
            entities = cur_entities  # 복구로 확장된 entity 를 downstream 에 반영.

            # 복구 소진 후에도 FAIL → 답할 자격 미달. 결정 (a): FAIL → refuse
            # INSUFFICIENT_EVIDENCE(복구 시도함); WEAK → 진행(Node 15 claim gate 가
            # backstop). PASS → 진행.
            if evaluation.overall_decision == GateDecision.FAIL.value:
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.INSUFFICIENT_EVIDENCE, conf,
                    error_code="evaluate_fail_after_recover",
                    evaluation=evaluation, recover_rounds=recover_rounds,
                    scope=scope,
                )

            # Node 8 — multi_hop_expand (수평 cross-ref, 결정론 regex, LLM 미사용).
            # leaf 텍스트의 §N.M 같은 같은-문서 섹션 참조를 추적해 fetch_section 으로
            # 가져온다. Node 9 *앞*에 둔다(좁히기 전 전체 텍스트가 ref recall 에 필요).
            # 가져온 hop chunk 는 컨텍스트 전용 추가(재게이트 없음). clause_id·외부문서
            # 참조는 v1 에서 clause_id 가 null 이라 v2-gated(미구현).
            await emit_step("multi_hop_expand", "started")
            hop_edges: list[HopEdge] = []
            hop_chunks = await self._multi_hop(chunks, ctx=ctx, record=record,
                                               edges_out=hop_edges)
            if hop_chunks:
                chunks = chunks + hop_chunks
            await emit_step("multi_hop_expand",
                            "ok" if hop_edges else "skipped",
                            num_hops=len(hop_edges), num_hop_chunks=len(hop_chunks))

            # Node 9 — evidence_snippet (문장 window 추출; LLM 미사용)
            await emit_step("evidence_snippet", "started")
            with _TRACER.start_as_current_span("agent.evidence_snippet") as s:
                oi.set_kind(s, oi.KIND_CHAIN)
                citation_ids_for_snippet = [f"cite-{i}" for i in range(len(chunks))]
                evidence_pack = self._snippet_extractor.extract(
                    chunks,
                    query_text=request.query_text,
                    entities=entities,
                    citation_ids=citation_ids_for_snippet,
                )
                # 추출된 window 로 각 chunk 의 snippet 을 교체 → 기존 ContextBuilder 가
                # 그대로 prompt 에 싣는다(ContextBuilder 변경 불필요, v2 공유 안전).
                window_by_chunk = {s.chunk_id: s.text for s in evidence_pack.snippets}
                chunks = [
                    # RetrievedChunk 는 pydantic(frozen) — dataclasses.replace 가 아니라
                    # model_copy(update=...) 로 교체.
                    c.model_copy(update={"snippet": window_by_chunk.get(c.chunk_id, c.snippet)})
                    for c in chunks
                ]
                s.set_attribute("snippet.num_snippets", len(evidence_pack.snippets))
                s.set_attribute("snippet.pack_hash", evidence_pack.pack_hash)
            await emit_step("evidence_snippet", "ok",
                            num_snippets=len(evidence_pack.snippets),
                            pack_hash=evidence_pack.pack_hash)

            # P1a Section auto-merge(수직) — Node 6 가 *불충분*(decision≠PASS) 으로
            # 본 leaf 를 그 Section 의 형제 문단으로 확장한다. 점수·게이트는 leaf 그대로
            # (재게이트 없음 — §4.1 분모 트랩 회피), snippet 만 Section 본문으로 치환.
            # leaf window 는 demote 복원용으로 스태시. local fetch 는 빈 결과라 no-op.
            await emit_step("section_merge", "started")
            chunks, leaf_window_stash, promoted_ids = await self._section_merge(
                chunks, evaluation, ctx=ctx, record=record,
            )
            # 승격 flag·정책 해시는 *실제로 병합된* chunk(=형제 fetch 성공, stash 존재)에만
            # 기록한다. promoted_ids(=non-PASS) 만으로 기록하면 local/형제부재 경로에서
            # "Section 병합이 일어났다"고 event 가 거짓 보고한다(감사 신뢰성).
            merged_ids = set(leaf_window_stash)
            if merged_ids:
                evaluation = replace(
                    evaluation,
                    per_chunk=tuple(
                        replace(sig, promote=(sig.chunk_id in merged_ids))
                        for sig in evaluation.per_chunk
                    ),
                )
            await emit_step("section_merge",
                            "ok" if merged_ids else "skipped",
                            num_promoted=len(promoted_ids),
                            num_merged=len(merged_ids))

            # P1b 예산 거버너 — context_token_budget>0 일 때만 활성(기본 0 → no-op,
            # 기존 동작 보존). 강등 순서: 승격→leaf 복원 → 최하위 drop → (Node 9 window).
            # 이후 lost-in-the-middle 재배치. 모든 강등·drop 은 로그(silent 금지).
            budget_log: list[str] = []
            if self._context_token_budget > 0:
                chunks = self._apply_context_budget(
                    chunks, leaf_window_stash, budget_log=budget_log,
                )
                if budget_log:
                    await emit_step("context_budget", "ok",
                                    budget=self._context_token_budget,
                                    actions=len(budget_log))

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
                # Node 9 window 가 주입된 chunk 를 RETRIEVER 타일에 노출(v2 parity).
                oi.set_retrieval_documents(
                    s,
                    [
                        {
                            "id": c.chunk_id,
                            "score": c.score,
                            "content": getattr(c, "snippet", None)
                            or getattr(c, "text", None)
                            or getattr(c, "content", ""),
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
                        error_code="prompt_profile_not_found", scope=scope,
                    )
                context_block = self._context_builder.render_for_prompt(pack)
                if self._citation_contract:
                    context_block = (
                        "# CITATION CONTRACT\n"
                        + self._citation_contract.strip()
                        + "\n\n"
                        + context_block
                    )
                # Node 6/7 이 WEAK(빈약) 로 귀결되면 *생성 전에* 그 진단 맥락을 프롬프트
                # 최상단에 실어, 답변이 "검색 근거가 왜 부족한지"를 스스로 설명하게 한다.
                # 스트리밍 후 사후검증은 이미 전송된 텍스트를 되돌릴 수 없으므로(생성-검증
                # 결합 결함) 한계 고지는 생성 시점에 들어가야 한다(CLAUDE.md §6).
                quality_note = self._retrieval_quality_note(evaluation, recover_rounds)
                if quality_note:
                    context_block = (
                        "# 검색 품질 경고 (RETRIEVAL QUALITY ADVISORY)\n"
                        + quality_note
                        + "\n\n"
                        + context_block
                    )
                rendered = self._renderer.render(
                    profile, query_text=request.query_text, context_block=context_block,
                )
                s.set_attribute("rendered_prompt_hash", rendered.rendered_prompt_hash)
                if self._citation_contract_sha:
                    s.set_attribute("citation_contract_sha", self._citation_contract_sha)
                s.set_attribute("retrieval_quality_advisory", quality_note is not None)
                if quality_note is not None:
                    s.set_attribute("retrieval_diagnosis",
                                    self._recoverer.diagnose(evaluation))
                oi.set_kind(s, oi.KIND_CHAIN)
                oi.set_io(
                    s,
                    input_value=request.query_text,
                    output_value={
                        "profile_id": rendered.profile_id,
                        "profile_version": rendered.profile_version,
                        "rendered_prompt_hash": rendered.rendered_prompt_hash,
                    },
                )
                await self._sink.write_prompt_render_record(
                    request.interaction_id,
                    self._renderer.to_record(rendered, query_text=request.query_text),
                )
                await self._sink.write_context_snapshot(
                    request.interaction_id, self._context_builder.to_snapshot(pack),
                )
            await emit_step("prompt_render", "ok", profile_id=rendered.profile_id,
                            profile_version=rendered.profile_version,
                            retrieval_advisory=quality_note is not None)

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
                    scope=scope,
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
            # 비활성(claim_verification_enabled=False) 시 SKIPPED 로 귀결될 기본값.
            # 이 변수들은 아래 enabled 분기에서만 덮어쓴다(양쪽 분기에서 정의 보장).
            claims: tuple = ()
            verification_status = VerificationStatus.SKIPPED.value
            entailment_model: str | None = None
            decompose_method: str | None = None
            citation_completeness = 0.0
            faithfulness = 0.0

            if not self._claim_verification_enabled:
                # 사후 claim 검증 비활성 — 생성 텍스트가 streaming 으로 이미 전송된
                # 구조에서 사후 검증이 답변을 되돌릴 수 없으므로 Node 14/15/16 skip.
                # verification_status=SKIPPED → response_format 의 else 분기로 떨어져
                # 답변·인용이 그대로 통과한다.
                await emit_step("claim_decompose", "skipped")
                await emit_step("claim_verify", "skipped")
            else:
                utility = self._utility_llm or llm  # Node 14/15 LLM(temperature 0)

                # Node 14 — claim_decompose
                await emit_step("claim_decompose", "started")
                decomposed = await ClaimDecomposer(utility).decompose(llm_result.text)
                if decomposed.method == "llm":
                    llm_calls_used += 1
                decompose_method = decomposed.method
                await emit_step("claim_decompose", "ok",
                                num_claims=len(decomposed.claims),
                                method=decomposed.method)

                # Node 15 — claim_verify (4-step per claim → 집계 status)
                await emit_step("claim_verify", "started")
                # 근거 매핑은 *위치(cite-i)*가 아니라 chunk_id 로, 그리고 *최종 chunk 의
                # snippet*(=프롬프트가 실제로 실은 텍스트)에서 가져온다. 두 가지를 동시에
                # 바로잡는다:
                #  (1) 위치 desync — citation_id 는 Node 9/ pack.build 에서 위치로 산출되어
                #      Section 병합·예산 drop·재배치·다홉 append 가 순서를 바꾸면 깨진다.
                #  (2) 근거 staleness — evidence_pack 은 Node 9 시점(leaf window)이라
                #      Section 병합 후 chunk.snippet(섹션 본문)과 어긋난다. 그대로 쓰면
                #      모델은 섹션을 보고 검증기는 window 를 봐서 형제-근거 claim 이 거짓
                #      unsupported 로 떨어진다. render_for_prompt 는 chunk.snippet 을 싣고
                #      (runner 는 capture_mode="snippets" 강제), 그 snippet 이 곧 모델이 본
                #      근거이므로 최종 chunks 에서 직접 만든다(hop chunk 포함).
                cite_by_chunk = {
                    c.chunk_id: c.citation_id for c in pack.citation_candidates
                }
                evidence_by_cite = {
                    cite_by_chunk[c.chunk_id]: (c.snippet or c.text or "")
                    for c in chunks
                    if c.chunk_id in cite_by_chunk and (c.snippet or c.text)
                }
                revision_by_cite = {
                    c.citation_id: c.revision for c in final_candidates if c.revision
                }
                verifier = ClaimVerifier(EntailmentChecker(utility))
                verify_res = await verifier.verify(
                    list(decomposed.claims),
                    resolvable_citation_ids=set(resolvable_citation_ids),
                    candidate_citation_ids={c.citation_id for c in pack.citation_candidates},
                    evidence_by_cite=evidence_by_cite,
                    version_constraint=query_plan.version_constraint,
                    revision_by_cite=revision_by_cite,
                )
                if verify_res.entailment_ran:
                    llm_calls_used += 1
                claims = verify_res.claims
                verification_status = verify_res.status
                entailment_model = (
                    EntailmentChecker(utility).model_id if verify_res.entailment_ran else None
                )
                # 이벤트 호환용 두 스칼라 — claim 집계에서 파생(구 _run_checks 대체).
                n_claims = max(1, len(claims))
                citation_completeness = sum(
                    1 for cv in claims if cv.checks.citation_resolves
                ) / n_claims
                faithfulness = sum(
                    1 for cv in claims if cv.status == ClaimStatus.SUPPORTED.value
                ) / n_claims
                await emit_step("claim_verify", "ok",
                                verification_status=verification_status,
                                num_claims=len(claims),
                                num_supported=sum(
                                    1 for cv in claims
                                    if cv.status == ClaimStatus.SUPPORTED.value),
                                contradicted=verify_res.contradicted,
                                entailment_ran=verify_res.entailment_ran)

            # Node 16 — selective_regenerate (PR-9 와 함께 배선). interim: partial/
            # unsupported 는 위에서 PARTIAL 로 귀결, contradicted 는 아래 response_format
            # 에서 VERIFICATION_FAILED refuse. 아직 국소 재작성 없음.
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
            # 안전 계약: 규제 근거 검증 축(verification_status 와 직교). v1 처럼
            # 규제 hard gate 미강제면 unverified — verification_status 가 PASS 여도
            # "규제 검증된 답변" 아님(PR-5 decision #3 의 전제).
            regulatory_grounding = (
                "verified" if evaluation.regulatory_enforced else "unverified"
            )
            with _TRACER.start_as_current_span("agent.response_format") as _rfmt:
                if verification_status == VerificationStatus.FAIL.value:
                    # contradicted claim 등 → 답변 폐기.
                    refusal = RefusalReason.VERIFICATION_FAILED.value
                    answer_text = _refusal_message(RefusalReason.VERIFICATION_FAILED)
                    citations: tuple[Citation, ...] = ()
                elif verification_status == VerificationStatus.PARTIAL.value:
                    refusal = RefusalReason.PARTIAL_ANSWER.value
                    answer_text = (
                        llm_result.text
                        + "\n\n[부분 답변] 일부 claim 의 근거·인용이 검증을 충족하지 못했습니다."
                    )
                    citations = _to_citations(final_candidates)
                else:
                    refusal = None
                    answer_text = llm_result.text
                    citations = _to_citations(final_candidates)
                # 미검증 규제 근거를 *답변 본문*에도 명시 — dumb client 도 보이게.
                if refusal is None and regulatory_grounding == "unverified":
                    answer_text = (
                        answer_text
                        + "\n\n[규제 근거 미검증] 현재 인덱스에 조문 ID·발효일·권위 등급"
                        " 메타가 없어 규제 차원 검증은 수행되지 않았습니다(인용 충실성만 검증)."
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
                    classification_confidence=conf,
                    classifier_backend=classification.classifier_backend,
                    entities=entities,
                    llm_id=llm_id,
                    model_id=llm_result.model_id,
                    claims=claims,
                    evaluation=evaluation,
                    recover_rounds=tuple(recover_rounds),
                    hops=tuple(hop_edges),
                    regulatory_grounding=regulatory_grounding,
                )
                oi.set_kind(_rfmt, oi.KIND_CHAIN)
                oi.set_io(
                    _rfmt,
                    input_value={
                        "verification_status": verification_status,
                        "regulatory_grounding": regulatory_grounding,
                    },
                    output_value={
                        "refusal_reason": refusal,
                        "num_citations": len(citations),
                        "answer_text": answer_text,
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
                    corpus_map_hash=scope.corpus_map_hash,
                    scope_mode=scope.mode,
                    evaluator_policy_hash=evaluation.evaluator_policy_hash,
                    regulatory_enforced=evaluation.regulatory_enforced,
                    per_chunk_signals=evaluation.per_chunk,
                    per_sub_question_decisions=evaluation.per_sub_question,
                    recover_rounds=tuple(recover_rounds),
                    hops=tuple(hop_edges),
                    evidence_pack_hash=evidence_pack.pack_hash,
                    section_merge_policy_hash=(
                        self._section_merge_policy_hash if merged_ids else None
                    ),
                    claims=claims,
                    verifier_policy_hash=None,
                    entailment_model=entailment_model,
                    decompose_method=decompose_method,
                    regulatory_grounding=regulatory_grounding,
                    budget=budget,
                )
                await self._recorder.persist(event)
                s.set_attribute("interaction_id", request.interaction_id)

            root.set_attribute("verification_status", verification_status)
            root.set_attribute("latency_ms", response.latency_ms)
            oi.set_io(root, output_value=response.answer_text)

        return response

    # ------------------------------------------------------------------
    # P1/P2 helpers — Section auto-merge(수직) · multi-hop(수평) · 예산 거버너.
    # 공통 계약: leaf 가 점수·게이트·인용의 단위. Section 병합은 snippet 치환(재게이트
    # 없음), hop 은 컨텍스트 전용 추가, 예산은 budget>0 일 때만.
    # ------------------------------------------------------------------
    @staticmethod
    def _chunk_tokens(c) -> int:
        if getattr(c, "token_count", None):
            return int(c.token_count)
        body = getattr(c, "snippet", None) or getattr(c, "text", None) or ""
        return len(body.split())

    async def _multi_hop(self, chunks, *, ctx, record, edges_out):
        """leaf 텍스트의 같은-문서 §N.M 참조를 fetch_section(prefix)으로 추적해
        새 chunk 를 모은다(컨텍스트 전용, 재게이트 없음). 호출 _MAX_HOPS 상한."""
        existing = {c.chunk_id for c in chunks}
        seen: set[tuple[str, str]] = set()
        hop_chunks: list = []
        for c in chunks:
            src = getattr(c, "source_id", None) or c.document_id
            if not src:
                continue
            body = (getattr(c, "text", None) or c.snippet or "")
            for m in _SECTION_REF_RE.finditer(body):
                if len(seen) >= _MAX_HOPS:
                    break
                sec = m.group(1)
                if (src, sec) in seen:
                    continue
                seen.add((src, sec))
                try:
                    res = await self._tools.invoke(
                        "document.fetch_section",
                        {"source_id": src, "section_key": sec,
                         "max_chunks": self._section_merge_max_chunks, "match": "prefix"},
                        ctx,
                    )
                except RequiredToolFailed:
                    continue  # required:false — 방어적.
                record(res)
                for fc in RetrieverSearchOutput.model_validate(res.output or {}).chunks:
                    if fc.chunk_id in existing:
                        continue  # 자기 자신·중복 제외.
                    existing.add(fc.chunk_id)
                    hop_chunks.append(fc)
                    edges_out.append(
                        HopEdge(from_chunk_id=c.chunk_id, ref_kind="section",
                                target_id=sec)
                    )
        return hop_chunks

    async def _section_merge(self, chunks, evaluation, *, ctx, record):
        """decision≠PASS 인 leaf 를 그 Section 형제 문단으로 *컨텍스트* 확장.
        snippet 만 Section 본문으로 치환(점수·인용 단위는 leaf 유지, 재게이트 없음).
        반환: (갱신 chunks, demote 복원용 leaf window stash, 승격 chunk_id 집합)."""
        promoted = {
            sig.chunk_id for sig in evaluation.per_chunk
            if sig.decision != GateDecision.PASS.value
        }
        if not promoted:
            return chunks, {}, set()
        stash: dict[str, tuple] = {}
        out: list = []
        for c in chunks:
            src = getattr(c, "source_id", None) or c.document_id
            if c.chunk_id not in promoted or not src or not getattr(c, "section_path", None):
                out.append(c)
                continue
            try:
                res = await self._tools.invoke(
                    "document.fetch_section",
                    {"source_id": src, "section_key": c.section_path[-1],
                     "max_chunks": self._section_merge_max_chunks, "match": "term"},
                    ctx,
                )
            except RequiredToolFailed:
                out.append(c)
                continue
            record(res)
            sibs = RetrieverSearchOutput.model_validate(res.output or {}).chunks
            if len(sibs) <= 1:
                out.append(c)  # 형제 없음(또는 자기 1건) → 확장 안 함.
                continue
            sibs = sorted(sibs, key=lambda s: _chunk_ordinal(s.chunk_id))
            assembled = "\n".join(
                (s.snippet or s.text or "") for s in sibs if (s.snippet or s.text)
            )
            if not assembled:
                out.append(c)
                continue
            stash[c.chunk_id] = (c.snippet, c.token_count)  # demote 복원용.
            out.append(
                c.model_copy(update={
                    "snippet": assembled,
                    "token_count": sum(self._chunk_tokens(s) for s in sibs),
                })
            )
        return out, stash, promoted

    def _apply_context_budget(self, chunks, stash, *, budget_log):
        """budget>0 일 때만 호출. 강등 순서: 승격→leaf 복원 → 최하위 drop →
        (Node 9 window 는 이미 적용). 이후 lost-in-the-middle 재배치(핵심 양끝)."""
        budget = self._context_token_budget
        chunks = list(chunks)
        total = sum(self._chunk_tokens(c) for c in chunks)
        # 1) 승격 chunk 를 stash 한 leaf window 로 복원(Section coherence 우선 양보).
        if total > budget:
            for i, c in enumerate(chunks):
                if total <= budget:
                    break
                if c.chunk_id in stash:
                    before = self._chunk_tokens(c)
                    leaf_snip, leaf_tok = stash[c.chunk_id]
                    chunks[i] = c.model_copy(
                        update={"snippet": leaf_snip, "token_count": leaf_tok}
                    )
                    total -= before - self._chunk_tokens(chunks[i])
                    budget_log.append(f"demote:{c.chunk_id}")
        # 2) 최하위(tail = 낮은 RRF / hop)부터 drop.
        while total > budget and len(chunks) > 1:
            dropped = chunks.pop()
            total -= self._chunk_tokens(dropped)
            budget_log.append(f"drop:{dropped.chunk_id}")
        # 3) lost-in-the-middle 재배치 — best 를 앞, 차선을 끝, 약한 것을 가운데.
        if len(chunks) > 2:
            head = chunks[::2]
            tail = chunks[1::2][::-1]
            chunks = head + tail
            budget_log.append("reorder:litm")
        return chunks

    # ------------------------------------------------------------------
    def _retrieval_quality_note(
        self, evaluation, recover_rounds: list[RecoverRound],
    ) -> str | None:
        """Node 6/7 이 WEAK 로 귀결될 때, 생성 LLM 에게 '검색 근거가 왜 약한지'를
        전달하는 결정론 advisory 를 만든다. PASS 면 None(FAIL 은 Node 7 후 이미
        refuse 되어 여기 도달하지 않는다).

        WEAK 는 '검색 실패'가 아니라 '근거 빈약'이다 — proceed-on-WEAK 는 의도된
        설계(Node 15 claim gate 가 backstop, run() L514~). 따라서 이 문구는 답변
        *거부* 가 아니라 '한계를 명시한 답변' 을 유도하도록 조정한다. 진단 라벨
        (diagnose) 만으로는 종종 'generic' 으로 떨어지므로, per_chunk 신호 집계를
        함께 실어 어느 차원이 약한지 구체화한다(advisor #3)."""
        if evaluation.overall_decision != GateDecision.WEAK.value:
            return None
        diagnosis = self._recoverer.diagnose(evaluation)
        reason = _RETRIEVAL_DIAGNOSIS_REASON.get(
            diagnosis, "검색 신호가 전반적으로 약합니다."
        )
        pc = evaluation.per_chunk
        n = max(1, len(pc))
        avg_lex = sum(c.s_lex for c in pc) / n
        avg_cov = sum(c.entity_coverage for c in pc) / n
        avg_reg = sum(c.s_reg for c in pc) / n
        avg_total = sum(c.s_total for c in pc) / n
        sq = evaluation.per_sub_question[0] if evaluation.per_sub_question else None
        counts = (
            f"통과 {sq.n_pass} · 경계 {sq.n_weak} · 미달 {sq.n_fail}"
            if sq else "집계 없음"
        )
        recover_note = (
            f"검색 복구를 {len(recover_rounds)}회 시도했으나 근거 품질이 기준에 미치지 못했습니다."
            if recover_rounds
            else "추가 검색 복구로도 개선 여지가 제한적이었습니다."
        )
        return (
            f"검색 게이트 평가 결과 이번 근거는 'WEAK(빈약)' 등급입니다(진단: {diagnosis}).\n"
            f"- 사유: {reason}\n"
            f"- 근거 {len(pc)}건 게이트 판정: {counts}\n"
            f"- 평균 신호(1.0 만점): 어휘일치 {avg_lex:.2f} · 엔티티커버리지 {avg_cov:.2f} · "
            f"규제권위 {avg_reg:.2f} · 종합 {avg_total:.2f}\n"
            f"- {recover_note}\n"
            "지침: 위 근거만으로 답변하되, 답변 서두에 어떤 측면(예: 핵심 용어·엔티티 "
            "매칭 부족, 규제 권위 근거 약함)에서 검색 근거가 부족한지와 그로 인한 답변의 "
            "한계를 1~2문장으로 명시하십시오. 근거로 확인되지 않은 사항은 단정하지 말고 "
            "확인되지 않았음을 분명히 구분하십시오. (이것은 답변 거부가 아니라, 한계를 "
            "투명하게 밝힌 답변을 요구하는 것입니다.)"
        )

    # ------------------------------------------------------------------
    async def _classify(self, request: AgentRequest) -> ClassificationResult:
        # Reuse the v2 Node 1 shim (ADR-0003).
        from app.application.agents.sequential.nodes.classify import classify
        return await classify(request, self._classifier)

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
                      verification_status: VerificationStatus = VerificationStatus.FAIL,
                      evaluation=None, recover_rounds=(),
                      scope: ScopeDecision | None = None):
        # 거부는 가장 의미 있는 모먼트인데 thinking 트레이스가 무음 종료되면 사용자는
        # "왜 답이 안 나왔나"를 알 수 없다 → 요약 채널에 거부 사유 1줄을 종결로 남긴다
        # (거부 메시지 본문은 answer_text 로 별도 전달).
        await emit_step("refused", "ok", reason=reason.value)
        # 평가 *후* refusal(예: INSUFFICIENT_EVIDENCE)은 per_chunk_signals·
        # recover_rounds·policy_hash 가 이미 존재하므로 event 에 실어야 한다 —
        # "왜 거부했나"가 규제 도메인 defensibility 의 핵심 질문(CLAUDE.md §5).
        evaluation_kwargs: dict[str, Any] = {}
        if evaluation is not None:
            evaluation_kwargs = dict(
                evaluator_policy_hash=evaluation.evaluator_policy_hash,
                regulatory_enforced=evaluation.regulatory_enforced,
                per_chunk_signals=evaluation.per_chunk,
                per_sub_question_decisions=evaluation.per_sub_question,
            )
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
            evaluation=evaluation,
            recover_rounds=tuple(recover_rounds),
        )
        scope_kwargs: dict[str, Any] = {}
        if scope is not None:
            # scope 적용 *후* 의 refusal(RETRIEVAL_NO_RESULT/INSUFFICIENT_EVIDENCE)
            # 은 "scope 가 막다른 벽으로 좁혔나"를 event 가 단독 설명하게 한다.
            scope_kwargs = dict(corpus_map_hash=scope.corpus_map_hash, scope_mode=scope.mode)
        event = self._recorder.build(
            request=request, response=response, agent_variant=self.spec.variant_id,
            started_at=started, tool_calls=tuple(tool_calls),
            classification_confidence=conf, error_code=error_code,
            recover_rounds=tuple(recover_rounds),
            **evaluation_kwargs,
            **scope_kwargs,
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
        utility_llm=deps.utility_llm,
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
        claim_verification_enabled=t.get("claim_verification_enabled", True),
        summarizer=deps.summarizer,
        retriever_top_k=t.get("retriever_top_k", 3),
        retriever_min_score=t.get("retriever_min_score", 0.0),
        retrieval_fetch_k=t.get("retrieval_fetch_k", 20),
        active_cells_mode=t.get("active_cells_mode", "all"),
        llm_call_budget=t.get("llm_call_budget", 8),
        citation_contract_path=t.get("citation_contract_path"),
        retrieval_planner=deps.retrieval_planner,
        retrieval_evaluator=deps.retrieval_evaluator,
        retrieval_recoverer=deps.retrieval_recoverer,
        regulatory_hard_gates_enforced=t.get("regulatory_hard_gates_enforced", False),
        corpus_map=deps.corpus_map,
        scope_tau_high=t.get("retrieval_scope_tau_high", 0.6),
        scope_tau_low=t.get("retrieval_scope_tau_low", 0.3),
        scope_min_token_count=t.get("retriever_min_token_count", 0),
        section_merge_max_chunks=t.get("section_merge_max_chunks", 50),
        context_token_budget=t.get("context_token_budget", 0),
    )
