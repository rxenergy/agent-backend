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
from app.application.agents.llm_router import LLMRouter, UnknownLLMError
from app.application.agents.registry import AgentDeps, register_variant
from app.application.context.pack import ContextBuilder, ContextPack
from app.application.events.recorder import EventRecorder
from app.application.intake.spec_driven_answer_spec import (
    SpecDrivenAnswerSpecInstantiator,
)
from app.application.intake.spec_driven_query import QueryFormulator
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
        citation_contract_path: str | None = None,
        retriever_top_k: int = 3,
        max_queries: int = 6,
        max_context_chunks: int = 8,
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
        self._top_k = retriever_top_k
        self._max_queries = max_queries
        self._max_context_chunks = max_context_chunks
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
                or self._generation_source is None:
            raise RuntimeError(
                "spec_driven_v1 prompt sources not wired — N1/N2/N4 prompts are "
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

            # === N1 Define Spec Node =====================================
            await emit_step("define_spec", "started")
            n1 = SpecDrivenAnswerSpecInstantiator(
                util,
                prompt_body=self._answer_spec_source.prompt_body,
                schema=self._answer_spec_source.schema or None,
                model_options=self._answer_spec_source.model_options or None,
                policy_hash=self._answer_spec_source.policy_hash,
            )
            spec = await n1.instantiate(request.query_text)
            await emit_step("define_spec", "ok", method=spec.instantiation_method,
                            num_slots=len(spec.required_slots),
                            num_refs=len(spec.explicit_references))

            # === N2 Query Formulation Node ===============================
            await emit_step("query_formulation", "started")
            n2 = QueryFormulator(
                util,
                prompt_body=self._query_source.prompt_body,
                schema=self._query_source.schema or None,
                model_options=self._query_source.model_options or None,
                policy_hash=self._query_source.policy_hash,
            )
            queries, formulation_method = await n2.formulate(request.query_text, spec)
            truncated = False
            if len(queries) > self._max_queries:
                truncated = True
                queries = queries[: self._max_queries]
            await emit_step("query_formulation", "ok", method=formulation_method,
                            num_queries=len(queries), truncated=truncated)

            # === N3 Retrieval (per-slot 멀티쿼리 + 병합) ==================
            await emit_step("retrieval", "started", num_queries=len(queries))
            chunks_by_id: dict[str, RetrievedChunk] = {}
            per_query_counts: list[int] = []
            with _TRACER.start_as_current_span("agent.retrieval") as rs:
                for q in queries:
                    out = await self._tools.invoke(
                        _SEARCH_TOOL,
                        {"query_text": q.query_text, "top_k": self._top_k,
                         "target": q.target},
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
                rs.set_attribute("retrieval.num_chunks", len(chunks_by_id))
                oi.set_kind(rs, oi.KIND_RETRIEVER)
            merged = sorted(chunks_by_id.values(), key=lambda c: c.score, reverse=True)
            # post-merge top-K cap(설계 §3.2) — 소형모델 컨텍스트 보호. no silent cap:
            # 절단 여부를 핀에 기록한다.
            chunks_capped = len(merged) > self._max_context_chunks
            chunks = merged[: self._max_context_chunks]
            evidence_gap = not chunks
            await emit_step("retrieval", "ok", num_chunks=len(chunks),
                            merged=len(merged), capped=chunks_capped,
                            evidence_gap=evidence_gap)

            # 재현 핀(원칙 5) — spec→query→retrieval 경로를 query_understanding 백에.
            qu_pin: dict[str, Any] = {
                "spec_driven": {
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
                             "target": q.target} for q in queries
                        ],
                    },
                    "retrieval": {
                        "num_chunks": len(chunks),
                        "merged": len(merged),
                        "capped": chunks_capped,
                        "per_query_counts": per_query_counts,
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
                "검색에서 근거를 한 건도 찾지 못했다. 사전 지식·기억으로 규제 사실을 "
                "지어내 답하지 마라(인용 마커도 쓰지 마라 — 근거 없음). 다음만 기술하라: "
                "(1) 어떤 명시적 참조·키워드로 찾았는지, (2) 무엇을 확인하지 못했는지, "
                "(3) 방어 가능한 답을 위해 무엇이 더 필요한지. Confidence 는 낮다고 명시하라."
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
        citation_contract_path=t.get("citation_contract_path"),
        retriever_top_k=t.get("retriever_top_k", 3),
        max_queries=t.get("spec_driven_max_queries", 6),
        max_context_chunks=t.get("spec_driven_max_context_chunks", 8),
    )
