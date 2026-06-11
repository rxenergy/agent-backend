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
    LazyReasoning,
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
from app.application.context.pack import ContextBuilder, ContextPack
from app.application.events.recorder import EventRecorder
from app.application.intake.spec_driven_answer_spec import (
    SpecDrivenAnswerSpecInstantiator,
)
from app.application.intake.spec_driven_query import QueryFormulator
from app.application.intake.spec_driven_triage import SpecDrivenTriage
from app.domain.agents import VariantSpec
from app.domain.errors import RefusalReason, VerificationStatus
from app.domain.interaction import AgentRequest, AgentResponse, Citation, ToolCallRecord
from app.domain.retrieval import RetrievedChunk
from app.domain.spec_driven import AnswerSpec, FormulatedQuery
from app.observability import openinference as oi
from app.observability.metrics import get_metrics
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMResult, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

SPEC_DRIVEN_VARIANT_ID = "spec_driven_v1"
_SEARCH_TOOL = "retrieval.search"
# 인덱싱 단계에서 표시한 노이즈 chunk(noise:true — 목차·헤더·fragment 등) 를 검색
# 모집단에서 hard-scope 로 제외(filters → OpenSearch term). local retriever 는 무시.
_NOISE_FILTER: dict[str, Any] = {"noise": False}
# gap-answer(0-chunk)에서 모델이 무근거로 남긴 인용 마커 제거용(결정=코드 안전망).
_CITE_RE = re.compile(r"\s*\[cite-\d+\]")


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class SpecDrivenRunner:
    """spec_driven_v1 — 검색 *앞단* 두 모델 노드를 둔 **선형** 검색 Agent
    (docs/plans/spec_driven_agent.design.v1.md).

    ReAct 루프도 결정론 16-노드도 아닌 4-노드 선형:
      N1 Define Spec Node — 원질의 → 답변 사양(의도·명시적 참조[리터럴]·근거 슬롯·
         권위 등급·논리 구조). utility LLM + json_schema.
      N2 Query Formulation Node — 사양 → 슬롯별 구체 검색쿼리(리터럴 키워드 + 명시적
         참조 verbatim, collection boost). utility LLM + json_schema.
      N3 Retrieval — 쿼리별 retrieval.search 실행 후 chunk 병합(dedup, score max).
      N4 Generation — (원질의 + 답변 사양 + chunks) → 명확한 출처·논리 구조 답변.

    근거 0건이면 거부 대신 **gap-answer**(사용자 결정 #3)하되, 사전 지식으로 규제 사실을
    지어내지 못하게 N4 프롬프트가 parametric 답변을 hard-forbid 한다(CLAUDE.md #6 불변식
    호환). 재현성·통제된 도구·실패 1급 불변식은 유지한다. v1 은 coverage 재검색·검증을
    두지 않는다(stub — 설계 §4.2/§11)."""

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
        answer_spec_source: Any = None,
        query_source: Any = None,
        generation_source: Any = None,
        triage_source: Any = None,
        general_source: Any = None,
        citation_contract_path: str | None = None,
        retriever_top_k: int = 3,
        max_queries: int = 10,
        max_context_chunks: int = 24,
        min_token_count: int = 0,
    ) -> None:
        self.spec = spec
        self._llm_router = llm_router
        self._utility_llm = utility_llm
        self._tools = tool_executor
        # snippets 모드 — chunk window 가 생성 프롬프트 evidence 로 닿게(react/v4 동형).
        self._context_builder = ContextBuilder(capture_mode="snippets")
        self._recorder = recorder
        self._sink = event_sink
        self._app_profile = app_profile
        self._answer_spec_source = answer_spec_source
        self._query_source = query_source
        self._generation_source = generation_source
        self._triage_source = triage_source
        self._general_source = general_source
        self._top_k = retriever_top_k
        self._max_queries = max_queries
        self._max_context_chunks = max_context_chunks
        # 노이즈 floor(Layer 2) — 본문 토큰 < N chunk(목차·헤더·fragment) 제외.
        # 0=비활성(기본). spec_driven 은 retrieval.scope 도구를 우회하므로 v3.1/react 와
        # 달리 settings/corpus_map 의 floor 가 닿지 않는다 → runner 가 직접 search 에 싣는다.
        self._min_token_count = min_token_count
        self._citation_contract: str | None = None
        if citation_contract_path:
            from pathlib import Path

            p = Path(citation_contract_path)
            if p.is_file():
                self._citation_contract = p.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Streaming wrapper — react_minimal/v4 와 동일 패턴(검증됨).
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
    # 4-Node 선형 conductor.
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

        if self._answer_spec_source is None or self._query_source is None \
                or self._generation_source is None \
                or self._triage_source is None or self._general_source is None:
            raise RuntimeError(
                "spec_driven_v1 prompt sources not wired — N0/N1/N2/N4/N4-G prompts are "
                "registry-hosted (prompts/registry.yaml spec_driven_* blocks)"
            )

        try:
            llm_id, llm = self._llm_router.resolve(request.model or None)
        except UnknownLLMError:
            llm_id, llm = self._llm_router.resolve(None)
        util = self._utility_llm or llm

        with _TRACER.start_as_current_span("agent.run") as root:
            root.set_attribute("interaction_id", request.interaction_id)
            root.set_attribute("agent.variant", self.spec.variant_id)
            root.set_attribute("llm_id", llm_id)
            oi.set_kind(root, oi.KIND_AGENT)
            oi.set_io(root, input_value=request.query_text)

            ctx = ToolExecutionContext(
                interaction_id=request.interaction_id, trace_id="",
                app_profile=self._app_profile, agent_variant=self.spec.variant_id,
                session_id=request.session_id, user_id=request.user_id,
                project_id=request.project_id,
            )

            # === N0 Triage Node (라우팅 판정 — 소형 모델 단독, 결정론 룰 없음) ====
            await emit_step("triage", "started")
            n0 = SpecDrivenTriage(
                util,
                prompt_body=self._triage_source.prompt_body,
                schema=self._triage_source.schema or None,
                model_options=self._triage_source.model_options or None,
                policy_hash=self._triage_source.policy_hash,
            )
            triage = await n0.triage(request.query_text)
            triage_pin: dict[str, Any] = {
                "route": triage.route,
                "references_specifics": triage.references_specifics,
                "rationale": triage.rationale,
                "method": triage.triage_method,
                "policy_hash": triage.policy_hash,
            }
            await emit_step("triage", "ok", route=triage.route,
                            method=triage.triage_method,
                            references_specifics=triage.references_specifics)
            root.set_attribute("spec_driven.route", triage.route)
            # thinking — 라우팅 판정 *근거*를 모델 산출(triage.rationale)로 전달한다. 이
            # variant 은 step renderer 를 우회하므로(thinking_renderer._LLM_THINKING_VARIANTS)
            # runner 가 reasoning 이벤트로 직접 Thought 블록에 싣되, route enum/플래그를
            # 재서술한 정해진 텍스트가 아니라 모델이 쓴 판정 사유를 그대로 보인다. native
            # CoT 가 없는 onprem 소형 모델에서도 rationale 은 모델 출력이라 가시.
            if triage.rationale:
                await emit_reasoning(f"\n**질의 분류**\n{triage.rationale}\n")

            # general 분기: N1/N2/N3 우회 → 모델 추론 직답(retrieval.search 0회).
            if triage.route == "general":
                return await self._run_general(
                    request, started, tool_calls, llm=llm, llm_id=llm_id,
                    triage_pin=triage_pin,
                )

            # === N1 Define Spec Node =====================================
            await emit_step("define_spec", "started")
            n1 = SpecDrivenAnswerSpecInstantiator(
                util,
                prompt_body=self._answer_spec_source.prompt_body,
                schema=self._answer_spec_source.schema or None,
                model_options=self._answer_spec_source.model_options or None,
                policy_hash=self._answer_spec_source.policy_hash,
            )
            spec = await n1.instantiate(request.query_text,
                                        reasoning_label="답변 사양 정의")
            await emit_step("define_spec", "ok", method=spec.instantiation_method,
                            num_slots=len(spec.required_slots),
                            num_refs=len(spec.explicit_references))
            # thinking 은 N1 LazyReasoning 이 native CoT(없으면 모델 rationale 필드)로
            # `**답변 사양 정의**` 아래 직접 싣는다 — runner 가 카운트를 재서술하지 않는다.

            # === N2 Query Formulation Node ===============================
            await emit_step("query_formulation", "started")
            n2 = QueryFormulator(
                util,
                prompt_body=self._query_source.prompt_body,
                schema=self._query_source.schema or None,
                model_options=self._query_source.model_options or None,
                policy_hash=self._query_source.policy_hash,
            )
            queries, formulation_method = await n2.formulate(
                request.query_text, spec, reasoning_label="검색 쿼리 생성")
            truncated = False
            if len(queries) > self._max_queries:
                truncated = True
                queries = queries[: self._max_queries]
            await emit_step("query_formulation", "ok", method=formulation_method,
                            num_queries=len(queries), truncated=truncated)
            # thinking 은 N2 LazyReasoning 이 native CoT(없으면 모델 rationale 필드)로
            # `**검색 쿼리 생성**` 아래 직접 싣는다 — runner 가 개수를 재서술하지 않는다.

            # === N3 Retrieval (per-slot 멀티쿼리 + 병합) ==================
            # N3 은 LLM 노드가 아니라 모델 출력이 없다 — thinking 에 결정론 텍스트를 싣지
            # 않는다(검색 단계·결과는 step/tool 사이드채널·OTel span 으로 흐른다). 근거
            # 유무는 N4 프롬프트의 EVIDENCE GAP 블록을 통해 모델 답변/CoT 에 반영된다.
            await emit_step("retrieval", "started", num_queries=len(queries))
            chunks_by_id: dict[str, RetrievedChunk] = {}
            # 슬롯 귀속 유지(merge 로 소실되던 정보) — per-slot floor 의 입력.
            slots_by_chunk: dict[str, set[str]] = {}
            per_query_counts: list[int] = []
            # per-query fetch 깊이 — context 예산(max_context_chunks)을 *단일 쿼리로도*
            # 채울 수 있게 budget 만큼 fetch(merge·dedup·floor 가 best 를 고름). retriever
            # operating point(하이브리드 가중치)는 config retriever_top_k 로 별도 선택되므로
            # per-call top_k(=fetch 깊이)를 키워도 가중치엔 영향 없음(profiles.py 주석).
            per_query_k = max(self._top_k, self._max_context_chunks)
            with _TRACER.start_as_current_span("agent.retrieval") as rs:
                for q in queries:
                    out = await self._tools.invoke(
                        _SEARCH_TOOL,
                        {"query_text": q.query_text, "top_k": per_query_k,
                         "target": q.target,
                         # 노이즈 floor(Layer 2) — 본문 토큰 < N chunk 제외. 0=비활성.
                         "min_token_count": self._min_token_count,
                         # 노이즈 제외(항상)에 쿼리별 collection hard-filter(모델이 filter
                         # 모드를 고른 경우만 q.filters 가 채워짐)를 합친다. noise 키는
                         # 스키마상 모델이 만들 수 없어 _NOISE_FILTER 가 shadow 되지 않는다.
                         "filters": {**_NOISE_FILTER, **q.filters}},
                        ctx,
                    )
                    record(out)
                    found = _parse_chunks(out.output if out.status == "success" else None)
                    per_query_counts.append(len(found))
                    for c in found:
                        # dedup by chunk_id, keep higher score.
                        prev = chunks_by_id.get(c.chunk_id)
                        if prev is None or c.score > prev.score:
                            chunks_by_id[c.chunk_id] = c
                        slots_by_chunk.setdefault(c.chunk_id, set()).add(q.slot_name)
                rs.set_attribute("retrieval.num_chunks", len(chunks_by_id))
                oi.set_kind(rs, oi.KIND_RETRIEVER)
            merged = sorted(chunks_by_id.values(), key=lambda c: c.score, reverse=True)
            # per-slot floor(설계 §3.2) — required 슬롯마다 최고 score chunk 1개를 먼저
            # 확보한 뒤 남은 예산을 score 순으로 채운다. 전역 top-K cap 이 required 근거를
            # 통째로 떨어뜨리던 문제 방지(no silent cap — coverage 를 핀에 기록).
            required_names = tuple(s.name for s in spec.required_slots if s.required)
            chunks, coverage = _select_with_slot_floor(
                merged, slots_by_chunk, required_names, self._max_context_chunks
            )
            chunks_capped = len(merged) > len(chunks)
            evidence_gap = not chunks
            await emit_step("retrieval", "ok", num_chunks=len(chunks),
                            merged=len(merged), capped=chunks_capped,
                            fetch_k=per_query_k, budget=self._max_context_chunks,
                            uncovered_required=len(coverage["uncovered_required"]),
                            evidence_gap=evidence_gap)

            # 재현 핀(원칙 5) — triage→spec→query→retrieval 경로를 query_understanding 백에.
            qu_pin: dict[str, Any] = {
                "spec_driven": {
                    "route": "retrieval",
                    "triage": triage_pin,
                    "spec": {
                        "intent": spec.intent,
                        "method": spec.instantiation_method,
                        "spec_hash": spec.spec_hash,
                        "policy_hash": spec.policy_hash,
                        "num_slots": len(spec.required_slots),
                        "explicit_references": list(spec.explicit_references),
                        "governing_normative_class": spec.governing_normative_class,
                    },
                    "formulation": {
                        "method": formulation_method,
                        "policy_hash": self._query_source.policy_hash,
                        "num_queries": len(queries),
                        "truncated": truncated,
                        "queries": [
                            {"slot": q.slot_name, "query_text": q.query_text,
                             "target": q.target, "filters": q.filters,
                             # mode 는 collection 이 어느 채널에 실렸는지에서 파생한다
                             # (별도 저장 없이 단일 진실원천 — 재현 핀이 자기서술적).
                             "mode": "filter" if q.filters.get("collection")
                             else ("boost" if q.target.get("collection") else "none")}
                            for q in queries
                        ],
                    },
                    "retrieval": {
                        "num_chunks": len(chunks),
                        "merged": len(merged),
                        "budget": self._max_context_chunks,
                        "fetch_k": per_query_k,
                        "capped": chunks_capped,
                        "per_query_counts": per_query_counts,
                        "min_token_count": self._min_token_count,
                        "filters": dict(_NOISE_FILTER),
                        "floored_slots": coverage["floored_slots"],
                        "covered_required_slots": coverage["covered_required"],
                        "uncovered_required_slots": coverage["uncovered_required"],
                    },
                    "evidence_gap": evidence_gap,
                }
            }

            # === N4 Generation ===========================================
            await emit_step("context_build", "started")
            with _TRACER.start_as_current_span("agent.context_build") as s:
                pack = self._context_builder.build(
                    interaction_id=request.interaction_id,
                    query_text=request.query_text,
                    chat_history=(), conversation_summary=None,
                    scenario_object="n_a", scenario_depth="n_a",
                    entities={}, chunks=chunks, memory_refs=(),
                    tool_result_refs=tuple(tool_result_refs),
                )
                s.set_attribute("context_hash", pack.context_hash)
                oi.set_kind(s, oi.KIND_RETRIEVER)
            await emit_step("context_build", "ok", context_hash=pack.context_hash)

            await emit_step("prompt_render", "started")
            rendered_text = self._render_generation_prompt(
                request.query_text, pack, spec, evidence_gap=evidence_gap
            )
            rendered_prompt_hash = _sha16(rendered_text)
            await self._sink.write_context_snapshot(
                request.interaction_id, self._context_builder.to_snapshot(pack),
            )
            await emit_step("prompt_render", "ok",
                            profile_id="spec_driven_generation_v1",
                            rendered_prompt_hash=rendered_prompt_hash)

            await emit_step("generation", "started", llm_id=llm_id,
                            evidence_gap=evidence_gap)
            llm_result = await self._generate(
                request, rendered_text, started, tool_calls, llm=llm,
                model_options=self._generation_source.model_options,
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
            chunk_ids = [c.chunk_id for c in chunks]
            # v1 은 검증 미수행(stub) → SKIPPED. gap 경로는 근거 0건이라 인용도 없음.
            terminal_outcome = "answer_with_gaps" if evidence_gap else "answer"
            answer_text = llm_result.text
            if evidence_gap:
                # 근거 0건인데 모델이 [cite-N] 을 남겼다면 제거(무근거 인용 차단 —
                # 프롬프트 hard-forbid 의 결정론 backstop, advisor #2).
                answer_text = _CITE_RE.sub("", answer_text).strip()

            response = AgentResponse(
                interaction_id=request.interaction_id,
                answer_text=answer_text,
                citations=citations,
                refusal_reason=None,  # gap-answer 는 거부 아님(사용자 #3).
                verification_status=VerificationStatus.SKIPPED.value,
                scenario_object="n_a", scenario_depth="n_a",
                latency_ms=int((time.monotonic() - started) * 1000),
                token_usage=dict(llm_result.token_usage),
                llm_id=llm_id, model_id=llm_result.model_id,
                regulatory_grounding="n_a",
            )
            metrics.record_terminal(outcome=terminal_outcome,
                                    latency_ms=response.latency_ms,
                                    scenario_object="n_a", scenario_depth="n_a")

            with _TRACER.start_as_current_span("event.persist") as s:
                event = self._recorder.build(
                    request=request, response=response,
                    agent_variant=self.spec.variant_id,
                    retrieved_chunk_ids=tuple(chunk_ids),
                    retrieval_confidence=(chunks[0].score if chunks else 0.0),
                    prompt_profile_id="spec_driven_generation_v1",
                    prompt_version=self._generation_source.prompt_version,
                    rendered_prompt_hash=rendered_prompt_hash,
                    prompt_composition_hash=self._generation_source.policy_hash,
                    prompt_source="local",
                    context_hash=pack.context_hash,
                    started_at=started,
                    tool_calls=tuple(tool_calls),
                    regulatory_grounding="n_a",
                    query_understanding=qu_pin,
                )
                await self._recorder.persist(event)
                s.set_attribute("interaction_id", request.interaction_id)

            return response

    # ------------------------------------------------------------------
    # N4-G General Generation — RAG 비대상 도메인 질의 직답(검색·도구·인용 없음).
    # N0 가 route=general 로 보낸 경우만. 1급 outcome=general_answer,
    # regulatory_grounding=parametric(grounded 답변과 감사상 구별 — 원칙 5·6).
    # ------------------------------------------------------------------
    async def _run_general(self, request: AgentRequest, started: float,
                           tool_calls: list[ToolCallRecord], *, llm: LLMPort,
                           llm_id: str, triage_pin: dict[str, Any]) -> AgentResponse:
        metrics = get_metrics()
        qu_pin: dict[str, Any] = {
            "spec_driven": {"route": "general", "triage": triage_pin}
        }

        # 빈 pack — context_hash·snapshot 을 retrieval 경로와 동형으로 남긴다(재현).
        await emit_step("context_build", "started")
        with _TRACER.start_as_current_span("agent.context_build") as s:
            pack = self._context_builder.build(
                interaction_id=request.interaction_id,
                query_text=request.query_text,
                chat_history=(), conversation_summary=None,
                scenario_object="n_a", scenario_depth="n_a",
                entities={}, chunks=[], memory_refs=(), tool_result_refs=(),
            )
            s.set_attribute("context_hash", pack.context_hash)
            oi.set_kind(s, oi.KIND_RETRIEVER)
        await emit_step("context_build", "ok", context_hash=pack.context_hash)

        await emit_step("prompt_render", "started")
        rendered_text = self._render_general_prompt(request.query_text)
        rendered_prompt_hash = _sha16(rendered_text)
        await self._sink.write_context_snapshot(
            request.interaction_id, self._context_builder.to_snapshot(pack),
        )
        await emit_step("prompt_render", "ok",
                        profile_id="spec_driven_general_v1",
                        rendered_prompt_hash=rendered_prompt_hash)

        await emit_step("generation", "started", llm_id=llm_id, route="general")
        llm_result = await self._generate(
            request, rendered_text, started, tool_calls, llm=llm,
            model_options=self._general_source.model_options,
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

        # 근거 0건이므로 무근거 [cite-N] 마커 제거(프롬프트 hard-forbid 의 결정론 backstop).
        answer_text = _CITE_RE.sub("", llm_result.text).strip()

        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text=answer_text,
            citations=(),
            refusal_reason=None,  # general 은 거부 아님.
            verification_status=VerificationStatus.SKIPPED.value,
            scenario_object="n_a", scenario_depth="n_a",
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage=dict(llm_result.token_usage),
            llm_id=llm_id, model_id=llm_result.model_id,
            regulatory_grounding="parametric",  # grounded 아님 — 감사 구별 핀.
        )
        metrics.record_terminal(outcome="general_answer", latency_ms=response.latency_ms,
                                scenario_object="n_a", scenario_depth="n_a")

        with _TRACER.start_as_current_span("event.persist") as s:
            event = self._recorder.build(
                request=request, response=response,
                agent_variant=self.spec.variant_id,
                retrieved_chunk_ids=(),
                retrieval_confidence=0.0,
                prompt_profile_id="spec_driven_general_v1",
                prompt_version=self._general_source.prompt_version,
                rendered_prompt_hash=rendered_prompt_hash,
                prompt_composition_hash=self._general_source.policy_hash,
                prompt_source="local",
                context_hash=pack.context_hash,
                started_at=started,
                tool_calls=tuple(tool_calls),  # general 은 비어 있음(도구 0회).
                regulatory_grounding="parametric",
                query_understanding=qu_pin,
            )
            await self._recorder.persist(event)
            s.set_attribute("interaction_id", request.interaction_id)

        return response

    def _render_general_prompt(self, query_text: str) -> str:
        """N4-G 프롬프트 — general body + 원질의 + 출력-언어 trailer(최고 recency).
        CONTEXT·ANSWER SPEC 블록 없음(근거 없는 추론 직답)."""
        parts = [self._general_source.prompt_body.strip()]
        parts.append("# QUERY\n" + query_text)
        parts.append(
            "# RESPONSE LANGUAGE\n"
            "Write the final answer in the same language as the QUERY above "
            "(Korean query → Korean answer)."
        )
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Generation prompt 합성 — spec trailer 주입 + (0-chunk 시)gap trailer.
    # ------------------------------------------------------------------
    def _render_generation_prompt(
        self, query_text: str, pack: ContextPack, spec: AnswerSpec, *,
        evidence_gap: bool,
    ) -> str:
        context_block = self._context_builder.render_for_prompt(pack)
        parts = [self._generation_source.prompt_body.strip()]
        if self._citation_contract:
            parts.append("# CITATION CONTRACT\n" + self._citation_contract.strip())
        parts.append("# CONTEXT\n" + context_block)
        parts.append("# ANSWER SPEC\n" + _render_spec_block(spec))
        if evidence_gap:
            # 0-chunk hard-forbid: 사전 지식 답변 금지(CLAUDE.md #6, advisor #1).
            parts.append(
                "# EVIDENCE GAP (NO RESULTS)\n"
                "Retrieval found no evidence at all. Do not fabricate regulatory facts "
                "from prior knowledge or memory (and do not use citation markers — there is "
                "no evidence). State only: (1) which explicit references / keywords you "
                "searched, (2) what you could not verify, (3) what more is needed for a "
                "defensible answer. State explicitly that confidence is low."
            )
        parts.append("# QUERY\n" + query_text)
        # 출력-언어 trailer — # QUERY *뒤*(최고 recency, react_minimal 와 동일 lesson).
        parts.append(
            "# RESPONSE LANGUAGE\n"
            "Write the final answer in the same language as the QUERY above "
            "(Korean query → Korean answer). Citation markers and source ids stay verbatim."
        )
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Generation — react_minimal/v4 패턴(LLM-agnostic, 스트리밍/non-streaming).
    # ------------------------------------------------------------------
    async def _generate(self, request, prompt_text, started, tool_calls, *,
                        llm: LLMPort, model_options: dict[str, Any] | None = None,
                        query_understanding: dict[str, Any] | None = None):
        # 생성 파라메터(temperature/max_tokens)는 호출자가 노드별 registry source
        # (N4=spec_driven_generation_v1, N4-G=spec_driven_general_v1)의 model_options
        # 로 넘긴다. 미전달 시 어댑터 하드코딩 기본(temperature=0.0, max_tokens=1024)
        # 으로 떨어져 선언값이 사문화되던 문제 수정.
        opts = dict(model_options or {})
        with _TRACER.start_as_current_span("llm.generation") as s:
            em = current_emitter()
            try:
                if em.active:
                    llm_result = await self._generate_stream(
                        llm, prompt_text, span=s, model_options=opts
                    )
                else:
                    llm_result = await llm.generate(
                        prompt_text, model_options=opts
                    )
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

    async def _generate_stream(self, llm: LLMPort, prompt: str, *, span,
                               model_options: dict[str, Any] | None = None) -> LLMResult:
        text_buf: list[str] = []
        token_usage: dict[str, int] = {}
        model_id: str | None = None
        # N4 native CoT 를 "답변 작성" 페이즈 헤더 아래로(reasoning 모델 한정; 없으면
        # 헤더도 안 뜸 — onprem Gemma 는 무음). reasoning 은 본문 토큰 이전 순서를
        # 상속한다(설계 §8 #1, #24295).
        lazy = LazyReasoning("답변 작성")
        async for delta in llm.generate_stream(prompt, model_options=model_options):
            if delta.content:
                text_buf.append(delta.content)
                await emit_token(delta.content)
            if delta.reasoning:
                await lazy.feed(delta.reasoning)
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
    # 거부 — 1급 outcome(원칙 6). spec_driven 은 LLM_UNAVAILABLE 만 거부한다
    # (0-chunk 은 gap-answer 로 진행 — 사용자 #3).
    # ------------------------------------------------------------------
    async def _refuse(self, request, started, tool_calls, reason: RefusalReason, *,
                      error_code: str | None,
                      query_understanding: dict[str, Any] | None = None):
        await emit_step("refused", "ok", reason=reason.value)
        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text=_refusal_message(reason),
            citations=(), refusal_reason=reason.value,
            verification_status=VerificationStatus.SKIPPED.value,
            scenario_object="n_a", scenario_depth="n_a",
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage={}, regulatory_grounding="n_a",
        )
        event = self._recorder.build(
            request=request, response=response, agent_variant=self.spec.variant_id,
            started_at=started, tool_calls=tuple(tool_calls), error_code=error_code,
            regulatory_grounding="n_a", query_understanding=query_understanding,
        )
        await self._recorder.persist(event)
        m = get_metrics()
        m.record_refusal(reason=reason.value)
        m.record_terminal(outcome="refused", latency_ms=response.latency_ms,
                          scenario_object="n_a", scenario_depth="n_a")
        return response


def _render_spec_block(spec: AnswerSpec) -> str:
    lines = [
        f"intent: {spec.intent}",
        f"answer_structure: {spec.answer_structure or '-'}",
        f"governing_normative_class: {spec.governing_normative_class or '-'}",
        f"explicit_references: {', '.join(spec.explicit_references) or '-'}",
        "required_slots:",
    ]
    for s in spec.required_slots:
        lines.append(f"- {s.name}: {s.description}".rstrip())
    return "\n".join(lines)


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


def _select_with_slot_floor(
    merged: list[RetrievedChunk],
    slots_by_chunk: dict[str, set[str]],
    required_names: tuple[str, ...],
    budget: int,
) -> tuple[list[RetrievedChunk], dict[str, list[str]]]:
    """per-slot floor 선택(설계 §3.2). `merged` 는 score 내림차순.

    1) **floor** — required 슬롯마다 (아직 미선택) 최고 score chunk 1개 확보. required
       근거가 전역 top-K 에 밀려 통째로 누락되는 것을 막는다.
    2) **fill** — 남은 예산을 score 순으로 채운다.

    렌더 순서는 score 내림차순(merged 순서) 유지. 슬롯 귀속은 N2 가 query.slot_name 을
    spec 슬롯명과 일치시키는 것에 의존한다(불일치 슬롯은 floor 대상에서 빠져 fill 로). budget
    이 required 슬롯 수보다 작으면 앞선 required 부터 floor 하고 나머지는 uncovered 로 남긴다.

    반환: (선택 chunk[score desc], coverage{floored_slots, covered_required,
    uncovered_required})."""
    selected: set[str] = set()
    floored_slots: list[str] = []
    # floor phase — required 슬롯 순서대로 최고 score 미선택 chunk 1개.
    for name in required_names:
        if len(selected) >= budget:
            break
        for c in merged:  # score desc → 첫 매칭이 최고 score
            if c.chunk_id in selected:
                continue
            if name in slots_by_chunk.get(c.chunk_id, ()):
                selected.add(c.chunk_id)
                floored_slots.append(name)
                break
    # fill phase — 남은 예산을 score 순으로.
    for c in merged:
        if len(selected) >= budget:
            break
        if c.chunk_id not in selected:
            selected.add(c.chunk_id)
    chunks = [c for c in merged if c.chunk_id in selected]  # score desc 유지
    covered = {s for cid in selected for s in slots_by_chunk.get(cid, ())}
    coverage = {
        "floored_slots": floored_slots,
        "covered_required": [n for n in required_names if n in covered],
        "uncovered_required": [n for n in required_names if n not in covered],
    }
    return chunks, coverage


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
    if reason is RefusalReason.LLM_UNAVAILABLE:
        return "응답이 지연되거나 모델을 가져올 수 없습니다. 잠시 후 다시 시도해 주세요."
    return "답변을 제공할 수 없습니다."


@register_variant(SPEC_DRIVEN_VARIANT_ID)
def _build_spec_driven(spec: VariantSpec, deps: AgentDeps) -> "SpecDrivenRunner":
    t = deps.tunables
    return SpecDrivenRunner(
        spec=spec,
        llm_router=deps.llm_router,
        tool_executor=deps.tool_executor,
        context_builder=deps.context_builder,
        recorder=deps.recorder,
        event_sink=deps.event_sink,
        app_profile=deps.app_profile,
        utility_llm=deps.utility_llm,
        answer_spec_source=deps.spec_driven_answer_spec_source,
        query_source=deps.spec_driven_query_source,
        generation_source=deps.spec_driven_generation_source,
        triage_source=deps.spec_driven_triage_source,
        general_source=deps.spec_driven_general_source,
        citation_contract_path=t.get("citation_contract_path"),
        retriever_top_k=t.get("retriever_top_k", 3),
        max_queries=t.get("spec_driven_max_queries", 10),
        max_context_chunks=t.get("spec_driven_max_context_chunks", 24),
        min_token_count=t.get("retriever_min_token_count", 0),
    )
