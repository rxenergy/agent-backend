from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
from app.application.agents.finder_loop import run_finder
from app.application.agents.llm_router import LLMRouter, UnknownLLMError
from app.application.agents.registry import AgentDeps, register_variant
from app.application.agents.sequential.nodes.classify import _HARDCODED_POLICY_HASH
from app.application.classification.active_cells import is_active
from app.application.classification.llm import REASON_LLM_UNAVAILABLE
from app.application.context.pack import ContextBuilder
from app.application.events.recorder import EventRecorder
from app.application.memory.policies import decide_session_injection
from app.application.memory.summarizer import ConversationSummarizer
from app.application.prompting.renderer import PromptRenderer, RenderedPrompt
from app.application.prompting.resolver import PromptResolver
from app.application.tool_runtime.executor import ToolExecutor
from app.domain.agents import VariantSpec
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
from app.domain.memory import MemoryRef, MemoryReviewStatus, StalenessStatus
from app.domain.retrieval import HopEdge
from app.observability import openinference as oi
from app.observability.metrics import get_metrics
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMResult, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

AGENTIC_FINDER_VARIANT_ID = "agentic_finder_v4"


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AgenticFinderRunner:
    """agentic_finder_v4 — 3-Phase **Intake → Retrieval → Generation** variant
    (docs/plans/agentic_finder_workflow.v1.md).

    conductor 가 3-Phase frame 을 배선하고 노드마다 step 이벤트를 emit 한다. 호출부를
    고정한 채 노드 body 만 교체 가능(원칙 #1). 노드 상태(구현/계획/stub) 표기:

      Phase 1 Intake
        • N0 query_translate        — 구현. 사용자 질의→query_en(검색용 영어) +
                                       source_language. 워크플로우 *내부*는 영어로
                                       구동(분류·answer_spec·Finder), 최종 출력만
                                       사용자 언어(N7 출력-언어 trailer). 프롬프트는
                                       registry 호스팅(sha 핀), 실패 시 원문 fallback.
        • N1 intent_classification  — 재사용(ClassificationPromptSource→LLMClassifier).
                                       query_en 으로 분류.
        • routing                   — 재사용(T3 메타 / T4 deflect / low-conf 명료화)
        • N1.5 terminology.canonicalize — 구현(P2). 용어집(ISO 25964 vocab.yaml) 결정론
                                       canonicalize, conductor-invoked(보장 실행). 분류
                                       entities → 정규형·정의를 컨텍스트에 *병기*(검색질의는
                                       query_en 유지). docs/plans/terminology_normalization_strategy.v1.md
        • N2 answer_spec            — 구현. 모델이 답변 사양(필요 슬롯·구조·깊이)을 산출
                                       (registry sha 핀, 실패 시 결정론 fallback).
      Phase 2 Retrieval
        • N3 finder_agent           — 구현. tool-calling 멀티턴 루프(run_finder):
                                       scope→search(+rerank)→submit_verdict, 검증=Finder
                                       LLM 단독, 종료=(verdict|recover_limit|max_turns).
                                       용어 정규화는 N1.5 로 상향됨(normalize 제거). 검색범위
                                       확장 terminology.expand(recover 전용)은 **계획**(P3).
        • N4 multi_hop_sequence     — STUB: hop_edges=() (인용 해소 미지원, finder §6)
      Phase 3 Generation
        • N5 memory_inject          — 재사용(session/approved gating)
        • N6 context_build          — 재사용(ContextBuilder, snippets 모드)
        • N7 prompt_render          — 재사용(PromptResolver/Renderer + citation contract
                                       + 출력-언어 trailer)
        • N8 generation             — 재사용(generate_stream 스트리밍)

    생성 답변 검증 = **비동기 audit만**(확정, finder §3) — 런타임 게이트 없음. audit
    잡(F-7)이 아직 없어 verification_status=SKIPPED 로 통과한다(audit 배선 후
    PENDING_AUDIT). 스트리밍으로 전송된 텍스트는 되돌릴 수 없으므로(생성-검증 결합
    결함) 차단이 아닌 관측·환류 목적이다."""

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
        utility_llm: LLMPort | None = None,
        classifier: Any | None = None,
        classification_threshold: float = 0.0,
        active_cells_mode: str = "all",
        summarizer: ConversationSummarizer | None = None,
        citation_contract_path: str | None = None,
        # N0 질의 번역 프롬프트 source(registry 호스팅, sha 핀). 워크플로우 내부는 영어,
        # 최종 출력만 사용자 언어. None 이면 N0 에서 부트 배선 오류(프롬프트 인라인 금지).
        query_translate_source: Any = None,
        # N7 출력-언어 지시문({language} 치환). 영어 컨텍스트로 추론하되 최종 답변은
        # 사용자 언어로 쓰도록 강제(citation contract 와 동일 prepend seam). None 이면
        # 미주입(지시문 없이 진행 — 시스템 프롬프트 언어를 따른다).
        output_language_contract_path: str | None = None,
        # N2 답변 사양 인스턴스화 프롬프트 source(registry 호스팅, sha 핀). None 이면
        # N2 에서 부트 배선 오류로 처리(프롬프트는 코드 인라인 금지 — 분류/정보요구와
        # 동일 fail-fast). v3.1 information_need_source 와 동일 idiom.
        answer_spec_source: Any = None,
        # N3 Finder 시스템 프롬프트 source(registry 호스팅, sha 핀 → finder_policy_hash).
        # None 이면 N3 에서 부트 배선 오류(프롬프트는 코드 인라인 금지).
        finder_source: Any = None,
        # Finder 루프 결정론 카운터(finder §2):
        # recover_limit=재검색 라운드 상한, max_turns=총 LLM 턴 backstop, hop depth.
        finder_recover_limit: int = 3,
        finder_max_turns: int = 10,
        multi_hop_depth: int = 3,
    ) -> None:
        self.spec = spec
        self._llm_router = llm_router
        self._utility_llm = utility_llm
        self._tools = tool_executor
        self._resolver = prompt_resolver
        self._renderer = prompt_renderer
        # finder §3 N6: snippets 모드(window 가 prompt evidence 로 닿게). v3.1 과 동형.
        self._context_builder = ContextBuilder(capture_mode="snippets")
        self._recorder = recorder
        self._sink = event_sink
        self._app_profile = app_profile
        self._classifier = classifier
        self._classification_threshold = classification_threshold
        self._active_cells_mode = active_cells_mode
        self._summarizer = summarizer
        self._query_translate_source = query_translate_source
        self._answer_spec_source = answer_spec_source
        self._finder_source = finder_source
        self._finder_recover_limit = finder_recover_limit
        self._finder_max_turns = finder_max_turns
        self._multi_hop_depth = multi_hop_depth
        # N7 citation contract preamble — 한 번 로드, context block 앞에 붙여
        # rendered_prompt_hash 에 반영. v3.1 과 동일 idiom.
        self._citation_contract: str | None = None
        self._citation_contract_sha: str | None = None
        if citation_contract_path:
            p = Path(citation_contract_path)
            if p.is_file():
                self._citation_contract = p.read_text(encoding="utf-8")
                self._citation_contract_sha = _sha16(self._citation_contract)
        # N7 출력-언어 지시문 템플릿(한 번 로드, {language} 는 런타임 치환). 영어
        # 컨텍스트로 추론하되 최종 답변은 사용자 언어로 쓰게 한다(생성-검증 결합 결함상
        # 스트리밍은 되돌릴 수 없으므로 *프롬프트 시점* 제어가 유일한 출력-언어 게이트).
        self._output_language_template: str | None = None
        self._output_language_sha: str | None = None
        if output_language_contract_path:
            p = Path(output_language_contract_path)
            if p.is_file():
                self._output_language_template = p.read_text(encoding="utf-8")
                self._output_language_sha = _sha16(self._output_language_template)

    # ------------------------------------------------------------------
    # Streaming wrapper — v2/v3.1 과 동일 패턴(검증됨).
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
    # 3-Phase conductor.
    # ------------------------------------------------------------------
    async def run(self, request: AgentRequest) -> AgentResponse:
        started = time.monotonic()
        metrics = get_metrics()
        tool_calls: list[ToolCallRecord] = []
        tool_result_refs: list[str] = []
        llm_calls_used = 0

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

            # === Phase 1 Intake ===========================================
            # N0 — query_translate. 워크플로우 *내부*(분류·answer_spec·Finder 검색)는
            # 영어 코퍼스를 대상으로 하므로 사용자 질의를 검색용 영어(query_en)로 한 번
            # 번역하고 원 언어(source_language)를 식별한다 — 최종 답변(N8)을 그 언어로
            # 되돌리기 위함. 이후 내부 노드는 query_en 으로 구동하고(internal_request),
            # 세션/이벤트/응답 echo 는 원문(request.query_text)을 보존한다. 프롬프트는
            # registry 호스팅(sha 핀) — source 미주입은 부트 배선 오류(인라인 금지).
            await emit_step("query_translate", "started")
            if self._query_translate_source is None:
                raise RuntimeError(
                    "query_translate_source not wired — N0 prompt is registry-hosted "
                    "(prompts/registry.yaml query_translate_prompts)"
                )
            translated = await self._query_translate_source.build_translator(
                self._utility_llm or llm
            ).translate(request.query_text)
            query_en = translated.query_en
            source_language = translated.source_language
            if translated.instantiation_method == "llm":
                llm_calls_used += 1
            # 내부 노드용 작업 요청 — query_text 만 영어로 치환(나머지 컨텍스트 보존).
            internal_request = replace(request, query_text=query_en)
            # 번역 재현 핀(원칙 #5) — 답변·거부·메타 모든 종단 이벤트에 동일하게 실어
            # "어떤 영어 질의·언어·정책으로 이 상호작용이 돌았나"를 단독 설명한다. 원질의는
            # request.query_text_hash 가 별도로 핀한다(여기는 내부 영어 질의·언어·정책).
            qu_pin: dict[str, Any] = {
                "query_translate": {
                    "query_en_hash": _sha16(query_en),
                    "source_language": source_language,
                    "method": translated.instantiation_method,
                    "policy_hash": translated.policy_hash,
                    "output_language_sha": self._output_language_sha,
                }
            }
            await emit_step("query_translate", "ok",
                            method=translated.instantiation_method,
                            source_language=source_language)

            # N1 — intent_classification (재사용, 영어 질의로 구동)
            await emit_step("intent_classification", "started")
            with _TRACER.start_as_current_span("agent.intent_classification") as s:
                classification = await self._classify(internal_request)
                scenario_object = classification.scenario_object
                scenario_depth = classification.scenario_depth
                entities = classification.entities
                conf = classification.confidence
                s.set_attribute("scenario_object", scenario_object)
                s.set_attribute("scenario_depth", scenario_depth)
                s.set_attribute("classification_confidence", conf)
                # Phoenix 단독 분석용 — confidence=0 원인을 상위 span 에서 바로 본다
                # (raw LLM 응답·outcome 은 자식 classification.llm_classify span).
                s.set_attribute("classifier.backend",
                                classification.classifier_backend or "")
                s.set_attribute("classifier.object_confidence",
                                classification.object_confidence)
                s.set_attribute("classifier.depth_confidence",
                                classification.depth_confidence)
                if classification.low_confidence_reason:
                    s.set_attribute("classifier.low_confidence_reason",
                                    classification.low_confidence_reason)
                oi.set_kind(s, oi.KIND_CHAIN)
                oi.set_io(s, input_value=request.query_text, output_value={
                    "scenario_object": scenario_object,
                    "scenario_depth": scenario_depth,
                    "confidence": conf,
                    "low_confidence_reason": classification.low_confidence_reason,
                    "object_confidence": classification.object_confidence,
                    "depth_confidence": classification.depth_confidence,
                    "entities": entities,
                })
            # LLM 백엔드 미도달(분류기 fallback=unavailable)은 "질문 모호"가 아니라
            # 가용성 장애다. "ok" emit(→ thinking "…이해했습니다") 이전에 단락해 오해
            # 라인을 막고 LLM_UNAVAILABLE 로 종결한다(API 가 OpenAI 에러로 변환).
            if classification.low_confidence_reason == REASON_LLM_UNAVAILABLE:
                await emit_step("intent_classification", "error",
                                error_code="llm_unavailable")
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.LLM_UNAVAILABLE, conf,
                    verification_status=VerificationStatus.SKIPPED,
                    error_code="llm_unavailable", classification=classification,
                    query_understanding=qu_pin,
                )
            await emit_step("intent_classification", "ok",
                            scenario_object=scenario_object,
                            scenario_depth=scenario_depth, confidence=conf)

            # routing — scope_tier 가 처리 계층을 먼저 가른다(검색 전 단락). T3 메타·
            # T4 deflect 는 short-circuit, low-conf 는 명료화 요청. v3.1 단락 패턴 재사용.
            await emit_step("scenario_routing", "started")
            scope_tier = classification.scope_tier
            if scope_tier == "T3":
                await emit_step("scenario_routing", "ok", scope_tier=scope_tier)
                return await self._meta_answer(request, started, tool_calls,
                                               classification, conf,
                                               query_understanding=qu_pin)
            if scope_tier == "T4":
                await emit_step("scenario_routing", "ok", scope_tier=scope_tier)
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.OUT_OF_SCOPE, conf,
                    verification_status=VerificationStatus.SKIPPED,
                    error_code="out_of_scope", classification=classification,
                    query_understanding=qu_pin,
                )
            if self._classifier is not None and conf < self._classification_threshold:
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.CLARIFICATION_REQUIRED, conf,
                    verification_status=VerificationStatus.SKIPPED,
                    error_code="classification_low_confidence",
                    classification=classification,
                    query_understanding=qu_pin,
                )
            inactive_cell = not is_active(
                scenario_object, scenario_depth, mode=self._active_cells_mode
            )
            ctx = ToolExecutionContext(
                interaction_id=request.interaction_id, trace_id="",
                app_profile=self._app_profile, agent_variant=self.spec.variant_id,
                session_id=request.session_id, user_id=request.user_id,
                project_id=request.project_id,
                scenario_object=scenario_object, scenario_depth=scenario_depth,
            )
            await emit_step("scenario_routing", "ok", scope_tier=scope_tier,
                            inactive_cell=inactive_cell)

            # N1.5 — terminology.canonicalize (conductor-invoked, 보장 실행). 분류
            # entities 를 후보 term 으로 넘겨 용어집(ISO 25964)으로 정규형·정의를 산출
            # (terminology_normalization_strategy.v1.md §3.2). 병기(annotate): 검색
            # 질의(query_en)는 불변, 정규형·정의를 Finder/생성 컨텍스트에 동반해 정밀도를
            # 돕는다(dense 임베딩 불변). LLM 재량이 아니라 워크플로우가 보장 호출한다
            # — 앞선 retrieval.normalize 의 "LLM 이 부를지 말지" 약점 해소.
            await emit_step("terminology_canonicalize", "started")
            flat_terms = [t for vals in (entities or {}).values() for t in (vals or [])]
            canon = await self._tools.invoke(
                "terminology.canonicalize",
                {"query_en": query_en, "terms": flat_terms}, ctx,
            )
            record(canon)
            canon_out = canon.output or {}
            terminology_annotation = _terminology_annotation(canon_out)
            qu_pin["terminology"] = {
                "vocab_sha": canon_out.get("vocab_sha"),
                "concept_ids": list(canon_out.get("concept_ids") or []),
                "num_canonical": len(canon_out.get("concept_ids") or []),
                "num_unresolved": len(canon_out.get("unresolved") or []),
            }
            await emit_step("terminology_canonicalize", "ok",
                            num_concepts=len(canon_out.get("concept_ids") or []),
                            num_unresolved=len(canon_out.get("unresolved") or []))

            # N2 — answer_spec. finder §3: "답변 사양"(필요 정보 슬롯·구조·깊이) =
            # N3 Finder 입력 계약. 슬롯은 *모델*이 질의별로 산출한다(표현=모델;
            # InformationNeedInstantiator 동형, 실패 시 결정론 fallback method 기록).
            # 프롬프트는 registry 호스팅(sha 핀) — source 미주입은 부트 배선 오류다
            # (silent degrade 금지 — 분류/정보요구와 동일 fail-fast).
            await emit_step("answer_spec", "started")
            if self._answer_spec_source is None:
                raise RuntimeError(
                    "answer_spec_source not wired — N2 prompt is registry-hosted "
                    "(prompts/registry.yaml answer_spec_prompts)"
                )
            answer_spec = await self._answer_spec_source.build_instantiator(
                self._utility_llm or llm
            ).instantiate(
                query_en,
                scenario_object=scenario_object,
                scenario_depth=scenario_depth,
                intent=classification.intent,
                entities=entities,
            )
            if answer_spec.instantiation_method == "llm":
                llm_calls_used += 1
            await emit_step("answer_spec", "ok", method=answer_spec.instantiation_method,
                            num_slots=len(answer_spec.required_slots),
                            answer_structure=answer_spec.answer_structure,
                            depth=answer_spec.depth)

            # === Phase 2 Retrieval ========================================
            # N3 — finder_agent. tool-calling 멀티턴 루프(scope→normalize→search→
            # submit_verdict)가 generate_with_tools 를 소비한다(llm_tool_calling §5).
            # 검증 = Finder LLM 단독(RRF·결정론 게이트 제거). 종료 = (verdict |
            # research_rounds≥recover_limit | max_turns backstop)지 raw 턴이 아니다
            # (두 문서가 반복 경고하는 핵심 불변). 프롬프트는 registry 호스팅(sha 핀)
            # — source 미주입은 부트 배선 오류.
            await emit_step("finder_agent", "started")
            if self._finder_source is None:
                raise RuntimeError(
                    "finder_source not wired — N3 system prompt is registry-hosted "
                    "(prompts/registry.yaml finder_prompts)"
                )
            finder_result = await run_finder(
                llm=llm,
                tool_executor=self._tools,
                ctx=ctx,
                system_prompt_body=self._finder_source.prompt_body,
                finder_policy_hash=self._finder_source.policy_hash,
                query_text=query_en,
                answer_spec=answer_spec,
                record=record,
                recover_limit=self._finder_recover_limit,
                max_turns=self._finder_max_turns,
                model_options=self._finder_source.model_options or None,
                terminology_annotation=terminology_annotation,
            )
            llm_calls_used += finder_result.llm_calls
            chunks: list[Any] = finder_result.chunks
            finder_rounds = finder_result.finder_rounds
            finder_recover_limit_hit = finder_result.recover_limit_hit
            await emit_step("finder_agent", "ok",
                            rounds=len(finder_rounds),
                            recover_limit_hit=finder_recover_limit_hit,
                            num_chunks=len(chunks),
                            verdict_sufficient=finder_result.verdict.get("sufficient"))

            # N4 — multi_hop_sequence (STUB). finder §6: 인용→문서 해소가 현재 인덱스
            # (nrc-all-v1, clause_id/outgoing-citation 필드 없음)에서 불가 → 유효
            # 도메인 객체(빈 hop_edges)를 산출하는 명시 stub. 코퍼스 적재·해소 방식
            # 확정 시 body 교체(Document Mapper).
            hop_edges: list[HopEdge] = self._multi_hop_stub(chunks)
            await emit_step("multi_hop_sequence", "ok", hops=len(hop_edges))

            # === Phase 3 Generation =======================================
            # N5 pre-step — session_load + approved_search + 주입 결정(재사용).
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

            approved = await self._tools.invoke(
                "memory.approved_search",
                {"query_text": query_en, "scenario_object": scenario_object,
                 "scenario_depth": scenario_depth, "top_k": 5}, ctx,
            )
            record(approved)
            decision = decide_session_injection(
                has_chat_history=bool(request.chat_history),
                prior_scenario_object=prior_so, prior_scenario_depth=prior_sd,
                current_scenario_object=scenario_object,
                current_scenario_depth=scenario_depth,
                prior_entities=prior_entities, current_entities=entities,
            )

            # N5 — memory_inject (재사용)
            await emit_step("memory_inject", "started")
            memory_refs: tuple[MemoryRef, ...] = ()
            memory_ids_used: list[str] = []
            memory_types_used: list[str] = []
            with _TRACER.start_as_current_span("agent.memory_inject") as s:
                oi.set_kind(s, oi.KIND_CHAIN)
                if decision.inject and session_load.output and session_load.output.get("present"):
                    sid = request.session_id or ""
                    memory_ids_used.append(sid)
                    memory_types_used.append("session")
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
                    memory_ids_used.append(mid)
                    memory_types_used.append("approved")
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
                s.set_attribute("memory.inject", decision.inject)
                s.set_attribute("memory.num_refs", len(memory_refs))
            await emit_step("memory_inject", "ok", inject=decision.inject,
                            num_memory_refs=len(memory_refs))
            metrics.record_memory_inject(inject=decision.inject)

            # N6 — context_build (재사용). F-0 은 chunks=() → no-evidence context.
            await emit_step("context_build", "started")
            with _TRACER.start_as_current_span("agent.context_build") as s:
                pack = self._context_builder.build(
                    interaction_id=request.interaction_id,
                    query_text=query_en,
                    chat_history=request.chat_history,
                    conversation_summary=conversation_summary if decision.inject else None,
                    scenario_object=scenario_object, scenario_depth=scenario_depth,
                    entities=entities, chunks=chunks, memory_refs=memory_refs,
                    tool_result_refs=tuple(tool_result_refs),
                )
                s.set_attribute("context_hash", pack.context_hash)
                oi.set_kind(s, oi.KIND_RETRIEVER)
                oi.set_io(s, input_value=request.query_text, output_value={
                    "context_hash": pack.context_hash, "num_chunks": len(chunks),
                    "num_memory_refs": len(memory_refs),
                })
            await emit_step("context_build", "ok", context_hash=pack.context_hash)

            # N7 — prompt_render (+ citation contract preamble) (재사용)
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
                        classification=classification,
                        query_understanding=qu_pin,
                    )
                context_block = self._context_builder.render_for_prompt(pack)
                if self._citation_contract:
                    context_block = (
                        "# CITATION CONTRACT\n"
                        + self._citation_contract.strip() + "\n\n" + context_block
                    )
                # N1.5 용어 정규화 병기 — 정규형·정의를 생성 컨텍스트에 동반(검색 질의는
                # 불변). citation contract 와 동일 prepend seam. rendered_prompt_hash 에
                # 반영(런타임 컨텐츠라 정적 프롬프트 sha 는 불변).
                if terminology_annotation:
                    context_block = (
                        "# TERMINOLOGY\n"
                        + terminology_annotation.strip() + "\n\n" + context_block
                    )
                # 출력-언어 지시문 — # QUERY 뒤(trailer, 최고 recency)에 둔다. 내부
                # 컨텍스트·질의는 영어(query_en)지만 프롬프트 *마지막*이 "최종 답변은
                # {language} 로" 라 모델이 영어 본문을 미러링하지 않게 가른다. {language}
                # 치환 후 rendered_prompt_hash 에 반영(재현 핀). 미주입이면 system 언어.
                output_language_trailer = (
                    self._output_language_template.replace("{language}", source_language)
                    if self._output_language_template else None
                )
                # 생성 프롬프트의 # QUERY 는 영어(query_en) — 추론은 영어로, 출력 언어는
                # trailer 가 가른다. 원문(request.query_text)은 세션/이벤트에 보존.
                rendered = self._renderer.render(
                    profile, query_text=query_en, context_block=context_block,
                    trailer=output_language_trailer,
                )
                s.set_attribute("rendered_prompt_hash", rendered.rendered_prompt_hash)
                if self._citation_contract_sha:
                    s.set_attribute("citation_contract_sha", self._citation_contract_sha)
                if self._output_language_sha:
                    s.set_attribute("output_language_sha", self._output_language_sha)
                    s.set_attribute("output_language", source_language)
                oi.set_kind(s, oi.KIND_CHAIN)
                oi.set_io(s, input_value=request.query_text, output_value={
                    "profile_id": rendered.profile_id,
                    "profile_version": rendered.profile_version,
                    "rendered_prompt_hash": rendered.rendered_prompt_hash,
                })
                await self._sink.write_prompt_render_record(
                    request.interaction_id,
                    # 렌더 본문이 query_en 을 담으므로 sidecar query_text 도 영어로 일치.
                    self._renderer.to_record(rendered, query_text=query_en),
                )
                await self._sink.write_context_snapshot(
                    request.interaction_id, self._context_builder.to_snapshot(pack),
                )
            await emit_step("prompt_render", "ok", profile_id=rendered.profile_id,
                            profile_version=rendered.profile_version)

            # N8 — generation (재사용, 스트리밍). finder §3 은 답변 사양(N2)을 프롬프트
            # 컨텍스트에 동반하라 하지만, F-0 은 answer_spec 을 *산출만* 하고 프롬프트
            # 주입은 미배선이다 — Generation 배선(N5–N8 + 이벤트 확장)은 F-6 이며 그때
            # answer_spec→renderer 스레딩을 추가한다(호출부 고정, body 교체).
            await emit_step("generation", "started", llm_id=llm_id)
            llm_result = await self._generate(
                request, rendered, started, tool_calls,
                scenario_object, scenario_depth, conf, llm=llm,
                classification=classification, query_understanding=qu_pin,
            )
            if isinstance(llm_result, AgentResponse):
                return llm_result  # LLM-unavailable refusal
            llm_calls_used += 1
            await emit_step("generation", "ok",
                            completion_tokens=llm_result.token_usage.get("completion_tokens", 0))
            metrics.record_tokens(
                prompt_tokens=int(llm_result.token_usage.get("prompt_tokens", 0)),
                completion_tokens=int(llm_result.token_usage.get("completion_tokens", 0)),
            )

            citations = _to_citations(pack.citation_candidates)
            chunk_ids = [getattr(c, "chunk_id", "") for c in chunks]

            # 생성 검증 = 비동기 audit 만(finder §3). F-0 은 audit 미배선 → SKIPPED
            # 로 통과(F-7 audit sidechannel 배선 후 PENDING_AUDIT). 런타임 게이트 없음.
            verification_status = VerificationStatus.SKIPPED.value

            # N5 post — session_update(재사용).
            new_turns = list(request.chat_history) + [
                ChatTurn(role="user", content=request.query_text)
            ]
            session_update = await self._tools.invoke(
                "memory.session_update",
                {
                    "session_id": request.session_id or "",
                    "recent_turns": [{"role": t.role, "content": t.content}
                                     for t in new_turns][-10:],
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
            with _TRACER.start_as_current_span("agent.response_format") as _rfmt:
                response = AgentResponse(
                    interaction_id=request.interaction_id,
                    answer_text=llm_result.text,
                    citations=citations,
                    refusal_reason=None,
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
                    hops=tuple(hop_edges),
                    regulatory_grounding="n_a",
                    classifier_intent=classification.intent,
                    scope_tier=classification.scope_tier,
                )
                oi.set_kind(_rfmt, oi.KIND_CHAIN)
                oi.set_io(_rfmt, input_value={"verification_status": verification_status},
                          output_value={"num_citations": len(citations),
                                        "answer_text": llm_result.text})

            metrics.record_terminal(outcome="answer", latency_ms=response.latency_ms,
                                    scenario_object=scenario_object,
                                    scenario_depth=scenario_depth)

            with _TRACER.start_as_current_span("event.persist") as s:
                event = self._recorder.build(
                    request=request, response=response,
                    agent_variant=self.spec.variant_id,
                    retrieved_chunk_ids=tuple(chunk_ids),
                    retrieval_confidence=(getattr(chunks[0], "score", 0.0)
                                          if chunks else 0.0),
                    prompt_profile_id=rendered.profile_id,
                    prompt_version=rendered.profile_version,
                    rendered_prompt_hash=rendered.rendered_prompt_hash,
                    prompt_composition_hash=rendered.composition_hash,
                    prompt_fragment_versions=dict(rendered.fragment_versions),
                    prompt_source=rendered.source,
                    context_hash=pack.context_hash,
                    classification_confidence=conf,
                    classifier_policy_hash=classification.classifier_policy_hash,
                    classifier_intent=classification.intent,
                    scope_tier=classification.scope_tier,
                    started_at=started,
                    tool_calls=tuple(tool_calls),
                    memory_ids_used=tuple(memory_ids_used),
                    memory_types_used=tuple(memory_types_used),
                    regulatory_grounding="n_a",
                    query_understanding=qu_pin,  # N0 번역 재현 핀(원칙 5).
                )
                await self._recorder.persist(event)
                s.set_attribute("interaction_id", request.interaction_id)

            # F-0: finder_rounds / answer_spec 은 산출·로그만 — InteractionEvent
            # 확장(finder_rounds[]/hop_edges[]/answer_spec_hash 재현성 핀)은 F-6.
            _ = (answer_spec, finder_rounds, finder_recover_limit_hit, llm_calls_used)
            return response

    # ------------------------------------------------------------------
    # Phase 1/2 node stubs (이후 PR 에서 body 교체, 호출부 고정).
    # ------------------------------------------------------------------
    def _multi_hop_stub(self, chunks: list[Any]) -> list[HopEdge]:
        """N4 STUB — 인용 해소 미지원(finder §6). 빈 hop_edges 를 산출하는 명시 stub.
        인터페이스: chunk → 인용 추출 → Document Mapper → 절차적 fetch → 누적."""
        return []

    # ------------------------------------------------------------------
    # Reused helpers (v2/v3.1 패턴).
    # ------------------------------------------------------------------
    async def _classify(self, request: AgentRequest) -> ClassificationResult:
        from app.application.agents.sequential.nodes.classify import classify
        return await classify(request, self._classifier)

    async def _generate(self, request, rendered: RenderedPrompt, started, tool_calls,
                        scenario_object, scenario_depth, conf, *, llm: LLMPort,
                        classification: ClassificationResult,
                        query_understanding: dict[str, Any] | None = None):
        with _TRACER.start_as_current_span("llm.generation") as s:
            em = current_emitter()
            try:
                if em.active:
                    llm_result = await self._generate_stream(llm, rendered.text, span=s)
                else:
                    llm_result = await llm.generate(rendered.text)
            except LLMUnavailableError:
                s.set_attribute("llm.status", "unavailable")
                return await self._refuse(
                    request, started, tool_calls, scenario_object, scenario_depth,
                    RefusalReason.LLM_UNAVAILABLE, conf, error_code="llm_unavailable",
                    verification_status=VerificationStatus.SKIPPED,
                    classification=classification, query_understanding=query_understanding,
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
                      verification_status: VerificationStatus = VerificationStatus.SKIPPED,
                      classification: ClassificationResult | None = None,
                      query_understanding: dict[str, Any] | None = None):
        await emit_step("refused", "ok", reason=reason.value)
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
            classifier_intent=classification.intent if classification else None,
            scope_tier=classification.scope_tier if classification else None,
        )
        classifier_policy_hash = (
            getattr(self._classifier, "policy_hash", None)
            if self._classifier is not None
            else _HARDCODED_POLICY_HASH
        )
        event = self._recorder.build(
            request=request, response=response, agent_variant=self.spec.variant_id,
            started_at=started, tool_calls=tuple(tool_calls),
            classification_confidence=conf, error_code=error_code,
            classifier_policy_hash=classifier_policy_hash,
            classifier_intent=classification.intent if classification else None,
            scope_tier=classification.scope_tier if classification else None,
            regulatory_grounding="n_a",
            query_understanding=query_understanding,  # N0 번역 핀(원칙 5).
        )
        await self._recorder.persist(event)
        m = get_metrics()
        m.record_refusal(reason=reason.value)
        m.record_terminal(outcome="refused", latency_ms=response.latency_ms,
                          scenario_object=scenario_object, scenario_depth=scenario_depth)
        return response

    async def _meta_answer(self, request, started, tool_calls,
                           classification: ClassificationResult, conf,
                           *, query_understanding: dict[str, Any] | None = None):
        """scope_tier=T3 — 역량·범위 메타 질의에 검색·인용 없이 응답(거부 아님)."""
        await emit_step("meta_answer", "ok", scope_tier=classification.scope_tier)
        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text=_META_CAPABILITY_TEXT,
            citations=(),
            refusal_reason=None,
            verification_status=VerificationStatus.SKIPPED.value,
            scenario_object=classification.scenario_object,
            scenario_depth=classification.scenario_depth,
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage={},
            regulatory_grounding="n_a",
            classifier_intent=classification.intent,
            scope_tier=classification.scope_tier,
        )
        classifier_policy_hash = (
            getattr(self._classifier, "policy_hash", None)
            if self._classifier is not None
            else _HARDCODED_POLICY_HASH
        )
        event = self._recorder.build(
            request=request, response=response, agent_variant=self.spec.variant_id,
            started_at=started, tool_calls=tuple(tool_calls),
            classification_confidence=conf, error_code=None,
            classifier_policy_hash=classifier_policy_hash,
            classifier_intent=classification.intent,
            scope_tier=classification.scope_tier,
            regulatory_grounding="n_a",
            query_understanding=query_understanding,  # N0 번역 핀(원칙 5).
        )
        await self._recorder.persist(event)
        get_metrics().record_terminal(
            outcome="answer", latency_ms=response.latency_ms,
            scenario_object=classification.scenario_object,
            scenario_depth=classification.scenario_depth,
        )
        return response


def _terminology_annotation(canon_out: dict[str, Any]) -> str | None:
    """N1.5 canonicalize 산출 → Finder/생성 컨텍스트 병기 텍스트. 정규화된 concept 가
    없으면 None(빈 헤더 금지). 미등록 passthrough term 은 제외하고 정규형+정의만 싣는다."""
    defs = canon_out.get("definitions") or {}
    canonical = canon_out.get("canonical_terms") or []
    unresolved = set(canon_out.get("unresolved") or [])
    seen: list[str] = []
    for t in canonical:
        if t in unresolved or t in seen:
            continue
        seen.append(t)
    if not seen:
        return None
    lines = ["## 용어 정규화(canonical terms — 검색·답변에 정규형 사용)"]
    for t in seen:
        d = defs.get(t)
        lines.append(f"- {t}" + (f": {d}" if d else ""))
    return "\n".join(lines)


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
    if reason is RefusalReason.UNKNOWN_SCENARIO:
        return "지원되지 않는 (시나리오, 깊이) 조합입니다. 다른 형태로 질문해 주세요."
    if reason is RefusalReason.LLM_UNAVAILABLE:
        return "응답이 지연되거나 모델을 가져올 수 없습니다. 잠시 후 다시 시도해 주세요."
    if reason is RefusalReason.OUT_OF_SCOPE:
        return (
            "이 시스템은 SMR(소형모듈원자로) 인허가·원자력 규제 질의에 한해 "
            "검색 근거로 답변합니다. 해당 도메인의 노형·규제·RAI 관련 질문으로 "
            "다시 시도해 주세요. (법적·인허가 자문 권위를 대신하지 않습니다.)"
        )
    return "근거가 부족하여 답변을 제공할 수 없습니다."


# scope_tier=T3 메타 응답 본문(고정 역량·범위 서술 — 검색 미수행).
_META_CAPABILITY_TEXT = (
    "저는 SMR(소형모듈원자로) 인허가·원자력 규제 도메인 QA 어시스턴트입니다.\n\n"
    "- **대상**: NRC 규제 지침(RG·SRP·DSRS·GDC), 10 CFR, NuScale FSAR/SAR, "
    "RAI/감사 기록.\n"
    "- **방식**: 인덱싱된 코퍼스를 검색해 근거(인용)와 함께 답합니다. "
    "근거가 없으면 답변을 보류하거나 제한적으로만 답합니다.\n"
    "- **한계**: 법적·인허가 자문 권위를 대신하지 않으며, 코퍼스 밖 사실을 "
    "지어내지 않습니다."
)


@register_variant(AGENTIC_FINDER_VARIANT_ID)
def _build_agentic_finder(spec: VariantSpec, deps: AgentDeps) -> "AgenticFinderRunner":
    t = deps.tunables
    # finder §3 N1: settings.classifier_backend 와 무관하게 registry 호스팅 프롬프트의
    # LLM 분류기로 고정(v3.1 과 동일 바인딩). source 미주입(테스트)이면 deps.classifier 폴백.
    classifier = deps.classifier
    if deps.classification_prompt_source is not None and deps.utility_llm is not None:
        classifier = deps.classification_prompt_source.build_classifier(deps.utility_llm)
    return AgenticFinderRunner(
        spec=spec,
        llm_router=deps.llm_router,
        tool_executor=deps.tool_executor,
        prompt_resolver=deps.prompt_resolver,
        prompt_renderer=deps.prompt_renderer,
        context_builder=deps.context_builder,
        recorder=deps.recorder,
        event_sink=deps.event_sink,
        app_profile=deps.app_profile,
        utility_llm=deps.utility_llm,
        classifier=classifier,
        classification_threshold=t.get("classification_threshold", 0.0),
        active_cells_mode=t.get("active_cells_mode", "all"),
        summarizer=deps.summarizer,
        citation_contract_path=t.get("citation_contract_path"),
        output_language_contract_path=t.get("output_language_contract_path"),
        query_translate_source=deps.query_translate_prompt_source,
        answer_spec_source=deps.answer_spec_prompt_source,
        finder_source=deps.finder_prompt_source,
        finder_recover_limit=t.get("finder_recover_limit", 3),
        finder_max_turns=t.get("finder_max_turns", 10),
        multi_hop_depth=t.get("multi_hop_depth", 3),
    )
