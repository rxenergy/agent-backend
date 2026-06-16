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
from app.application.memory.policies import (
    SessionInjectionDecision,
    decide_session_injection,
)
from app.domain.agents import VariantSpec
from app.domain.errors import RefusalReason, VerificationStatus
from app.domain.interaction import AgentRequest, AgentResponse, Citation, ToolCallRecord
from app.domain.memory import MemoryRef, MemoryReviewStatus, StalenessStatus
from app.domain.retrieval import RetrievedChunk, RetrieverSearchOutput
from app.domain.spec_driven import AnswerSpec, FormulatedQuery
from app.observability import openinference as oi
from app.observability.logging import get_logger
from app.observability.metrics import get_metrics
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMResult, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")
_LOG = get_logger("agent.spec_driven_v1")

SPEC_DRIVEN_VARIANT_ID = "spec_driven_v1"
_SEARCH_TOOL = "retrieval.search"
# 인덱싱 단계에서 표시한 노이즈 chunk(noise:true — 목차·헤더·fragment 등) 를 검색
# 모집단에서 hard-scope 로 제외(filters → OpenSearch term). local retriever 는 무시.
_NOISE_FILTER: dict[str, Any] = {"noise": False}
# gap-answer(0-chunk)에서 모델이 무근거로 남긴 인용 마커 제거용(결정=코드 안전망).
_CITE_RE = re.compile(r"\s*\[cite-\d+\]")


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _topic_signature(spec: AnswerSpec) -> str | None:
    """멀티턴 topic-shift 감지용 시그니처. N1 topic_label(모델 산출, 표현=모델) sha16 우선,
    없으면 결정론 fallback(governing_class + 정렬 explicit_references) — 둘 다 없으면 None
    (게이트는 양쪽 존재 시에만 비교하므로 None 은 topic 게이트를 비활성)."""
    if spec.topic_label:
        return _sha16(spec.topic_label.strip().lower())
    refs = "|".join(sorted(spec.explicit_references))
    basis = (spec.governing_normative_class or "") + "||" + refs
    return _sha16(basis) if refs or spec.governing_normative_class else None


def _scope_summary(q: FormulatedQuery) -> dict[str, Any]:
    """N2 쿼리의 스코프 채널별 (값, mode) 요약 + 무시/기각 감사(재현 핀 입력 — §6.5).
    각 채널은 boost(target) 또는 filter(filters) 중 한 곳에만 실린다. scope_audit 의
    status_dropped/design_dropped/canonical_id_rejected 를 그대로 노출한다(silent 금지)."""
    from app.application.intake.spec_driven_query import (
        _CANONICAL_FIELD,
        _DESIGN_FIELD,
        _STATUS_FIELD,
    )

    def _ch(field: str) -> dict[str, Any]:
        if field in q.filters:
            return {"value": q.filters[field], "mode": "filter"}
        if field in q.target:
            return {"value": q.target[field], "mode": "boost"}
        return {"value": None, "mode": "none"}

    out: dict[str, Any] = {
        "collection": _ch("collection"),
        "status": _ch(_STATUS_FIELD),
        "design": _ch(_DESIGN_FIELD),
        "canonical_id": _ch(_CANONICAL_FIELD),
    }
    # 감사 플래그는 값이 있을 때만 실어 핀을 군더더기 없이 유지(원칙 6 — 동작은 가시).
    out.update(q.scope_audit)
    return out


def _source_ids_of(chunks: list[RetrievedChunk],
                   fq_list: list[dict[str, Any]]) -> list[str]:
    """N5 retrieval_history 용 source_id 집합. 최종 청크의 document_id + follow-up
    target_source_ids 를 합쳐 dedup(검색 scope 힌트 — 후속 턴 재검색 우선순위 입력)."""
    out: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        # follow-up scope 와 정합하도록 source_id(ADAMS/packageId) 우선, 없으면 document_id.
        sid = getattr(c, "source_id", None) or getattr(c, "document_id", None)
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    for fq in fq_list:
        for sid in fq.get("target_source_ids", []) or []:
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


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
        max_context_chunks: int = 10,
        min_token_count: int = 0,
        context_token_budget: int = 0,
        follow_up_fetch_k: int = 8,
        follow_up_keep_k: int = 3,
        summarizer: Any = None,
        session_memory_enabled: bool = False,
        session_keep_turns: int = 10,
        session_retrieval_window: int = 5,
        session_overlap_threshold: float = 0.5,
    ) -> None:
        self.spec = spec
        self._llm_router = llm_router
        self._utility_llm = utility_llm
        self._tools = tool_executor
        # full 모드 — chunk *전문*이 생성 프롬프트 evidence 로 닿게 한다. 본문에서
        # 분리된 표([TABLE: tb_xxxx] 마커)가 snippet 캡에 잘리지 않고 ContextBuilder
        # 가 chunk.tables 로 인라인 치환할 수 있도록(spec_driven_table_inline_expansion).
        self._context_builder = ContextBuilder(capture_mode="full")
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
        # N4 생성 컨텍스트 토큰 예산(0=무제한). 1차 검색 전량 보존 + 2차 검색 score
        # 순 채움을 이 예산까지 — vLLM 윈도우 안전판(_assemble_final_chunks).
        self._context_token_budget = context_token_budget
        # N3.5 2차(follow-up) 검색 깊이 / 채택 수. fetch_k 는 참조 문서 내부에서
        # 가져올 후보 풀, keep_k 는 그 중 *쿼리당* 컨텍스트에 실을 상위 관련 청크 수
        # (관련성 게이트 — "필요·중요 내용만"). keep_k ≤ fetch_k.
        self._follow_up_fetch_k = follow_up_fetch_k
        self._follow_up_keep_k = follow_up_keep_k
        # 멀티턴 세션 메모리(drill-down 후속 질의 지원 — 설계 spec_driven_session_memory).
        # 기본 비활성(opt-in) → 단일턴 동작·기존 테스트 불변. enabled 시 N-1 session_load →
        # N0/N1 에 prior_context 동반(anaphora 해소) → N1.5 2단 게이트 → N4 CONVERSATION_SUMMARY
        # → N5 session_update(누적은 memory.session_update 도구 내부).
        self._summarizer = summarizer
        self._session_enabled = session_memory_enabled
        self._session_keep_turns = session_keep_turns
        self._session_retrieval_window = session_retrieval_window
        self._session_overlap_threshold = session_overlap_threshold
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

            # === N-1 Session Load (멀티턴 — opt-in) =======================
            # 직전 세션 상태를 적재하고 prior_context(요약+참조)를 만든다. 사전 게이트
            # (history/variant_switch)만으로 N0/N1 에 prior_context 를 동반할지 결정한다
            # (current route/authority/topic 는 아직 미확정 → 사후 게이트는 N1 뒤).
            sess = await self._session_load(request, ctx, record)
            prior_context = (
                self._build_prior_context(sess) if sess["pre_inject"] else None
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
            triage = await n0.triage(request.query_text, prior_context=prior_context)
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
                    triage_pin=triage_pin, ctx=ctx, sess=sess, record=record,
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
                                        reasoning_label="답변 사양 정의",
                                        prior_context=prior_context)
            await emit_step("define_spec", "ok", method=spec.instantiation_method,
                            num_slots=len(spec.required_slots),
                            num_refs=len(spec.explicit_references))

            # === N1.5 Inject Decision (사후 게이트 — 결정론) ==============
            # current route/authority/topic/refs(N0·N1 산출)까지 넣어 N4 주입·memory_ref
            # 기록의 최종 여부를 결정한다. 사전 통과/사후 차단(예: 권위 시프트)이면 N0/N1 은
            # 맥락을 봤지만 N4 evidence 맥락엔 싣지 않는다(해소하되 오염 차단).
            post = self._post_gate(request, sess, triage, spec)
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
            # 확보한 뒤 남은 예산을 score 순으로 채운다. 1차 검색 결과는 **전량 보존**
            # 한다(사용자 결정): budget=len(merged) → drop 없이 floor 정렬만 적용. 최종
            # 토큰 캡은 2차 병합 후 _assemble_final_chunks 가 일괄 적용한다(no silent cap).
            required_names = tuple(s.name for s in spec.required_slots if s.required)
            chunks, coverage = _select_with_slot_floor(
                merged, slots_by_chunk, required_names, len(merged)
            )
            # 1차 검색 청크 id — 최종 조립에서 always-included(전량 반영)의 입력.
            first_pass_ids = {c.chunk_id for c in chunks}
            evidence_gap = not chunks
            await emit_step("retrieval", "ok", num_chunks=len(chunks),
                            merged=len(merged),
                            fetch_k=per_query_k, budget=self._max_context_chunks,
                            uncovered_required=len(coverage["uncovered_required"]),
                            evidence_gap=evidence_gap)

            # N3.5 — follow-up 2차 검색 (외부 참조 문서 내 재검색).
            # 1차 검색 청크에서 외부 참조를 추출하고, 사용자 의도를 반영한 재검색 쿼리로
            # 참조 문서 내부를 다시 검색한다. 미배선(ToolUnknown)/실패 시 graceful skip.
            #
            # reasoning 방출 순서가 중요하다: 재검색 도구 + 2차 search 루프를 *먼저* 다
            # 돌려 요약(fq_summary)만 버퍼해 두고, `**참조 문서 재검색**` reasoning 은
            # 루프가 끝난 *뒤* 단 한 번 방출한다. 그래야 2차 search 의 tool 프레임이
            # reasoning_content 와 본문(N4 generation) 사이에 끼지 않아 OpenWebUI Thought
            # 블록이 조기 종결되지 않는다(#24295). 헤더 라벨은 N0 triage(질의 분류)와
            # 동형의 직접 emit_reasoning — 결정론 요약이라 LazyReasoning 불필요.
            await emit_step("follow_up_search", "started")
            follow_up_added = 0
            fq_summary: str | None = None
            # qu_pin(재현 핀)이 미배선/예외 경로에서도 follow_up 섹션을 균일하게 싣도록
            # try 밖에서 선초기화 — 어떤 skip 경로든 빈 리스트로 남는다.
            fq_list: list[dict[str, Any]] = []
            # 2차 검색 대상 쿼리 수(target_source_ids 보유분) — phase span 속성용.
            searchable_count = 0
            # N3.5 phase span — N3 의 agent.retrieval 과 대칭. 이게 없으면 Phoenix 에서
            # 참조 추출(retrieval.follow_up)·2차 search(retrieval.search) tool span 이
            # phase 부모 없이 agent.run 바로 밑에 흩어져, follow-up 단계를 묶어 분석할 수
            # 없다. CHAIN kind 로 추출→재검색 복합 단계를 표현하고, IO(질의·생성된 재검색
            # 쿼리 요약)와 카운터(생성·검색대상·채택 청크)를 span 에 실어 단계별 귀인을
            # 가능케 한다. self._tools.invoke 의 tool span 들은 이 with 컨텍스트 안에서
            # 생성돼 자동으로 이 span 의 자식으로 nesting 된다(병렬 gather 포함 — task
            # 생성 시점 컨텍스트가 캡처됨).
            with _TRACER.start_as_current_span("agent.follow_up_search") as fs:
                oi.set_kind(fs, oi.KIND_CHAIN)
                oi.set_io(fs, input_value=request.query_text)
                fs.set_attribute("follow_up.first_pass_chunks", len(chunks))
                fs.set_attribute("follow_up.fetch_k", self._follow_up_fetch_k)
                fs.set_attribute("follow_up.keep_k", self._follow_up_keep_k)
                try:
                    follow_up_res = await self._tools.invoke(
                        "retrieval.follow_up",
                        {
                            "query_text": request.query_text,
                            "chunks": [c.model_dump(mode="json") for c in chunks],
                        },
                        ctx,
                    )
                    record(follow_up_res)

                    if follow_up_res.status == "success" and follow_up_res.output:
                        fq_list = follow_up_res.output.get("follow_up_queries", []) or []
                        if fq_list:
                            # reasoning 텍스트는 버퍼만 — 아래 2차 search 가 끝난 뒤 방출.
                            fq_summary = "\n".join(
                                f"- {fq['query_text']} → {fq.get('target_source_ids', [])}"
                                for fq in fq_list
                            )

                            # target_source_ids 가 있는 쿼리만 2차 검색 대상(없으면 필터 불가).
                            searchable = [
                                fq for fq in fq_list if fq.get("target_source_ids")
                            ]
                            searchable_count = len(searchable)
                            # 2차 검색을 *병렬*로 실행한다(1차 추출 병렬화와 동일 취지). 각
                            # retrieval.search invoke 는 재진입 안전(span/httpx/encoder stateless)
                            # 이고, OpenSearch 어댑터가 torch 인코딩을 to_thread 로 풀어 동시
                            # 검색이 실제로 겹친다. record / chunks_by_id / follow_up_added 변이는
                            # race 방지를 위해 gather 완료 후 fq 원순서대로 *순차* 처리한다
                            # (dedup 우선순위·결정성 보존 → 직렬판과 동일 결과).
                            sub_results = await asyncio.gather(
                                *(
                                    self._tools.invoke(
                                        _SEARCH_TOOL,
                                        {
                                            "query_text": fq["query_text"],
                                            "top_k": self._follow_up_fetch_k,
                                            # 참조 문서 *내부* 로 모집단을 좁힌 hard-scope.
                                            # 노이즈 floor·noise:false 도 1차와 동일하게 실어
                                            # 목차·헤더·fragment 가 2차 결과를 오염시키지 않게
                                            # 한다(필요·중요 내용만 — 사용자 요구).
                                            "min_token_count": self._min_token_count,
                                            "filters": {
                                                **_NOISE_FILTER,
                                                "source_id": fq["target_source_ids"],
                                            },
                                        },
                                        ctx,
                                    )
                                    for fq in searchable
                                ),
                                return_exceptions=True,
                            )
                            for sub_res in sub_results:
                                if isinstance(sub_res, BaseException):
                                    # 개별 검색 실패는 graceful skip(형제 검색 비취소).
                                    continue
                                record(sub_res)
                                found = _parse_chunks(
                                    sub_res.output if sub_res.status == "success" else None
                                )
                                # 관련성 게이트(사용자 요구 — "필요·중요 내용만"). 2차 검색
                                # 점수는 follow-up 쿼리·대상 문서마다 척도가 달라 *전역* 절대
                                # 임계값이 부적절하다. 대신 쿼리 *내부* 상대순위 상위
                                # _follow_up_keep_k 개만 채택한다(검색이 score desc 로 반환).
                                # 나머지(저관련 tail)는 컨텍스트에서 배제 → 참조 문서에서
                                # 의도와 무관한 단락이 답변을 희석하지 않는다.
                                for c in found[: self._follow_up_keep_k]:
                                    if c.chunk_id not in chunks_by_id:
                                        chunks_by_id[c.chunk_id] = c
                                        follow_up_added += 1

                            if follow_up_added > 0:
                                # 1차+2차 통합 재정렬(score desc). 최종 chunk 선택은 try
                                # 밖에서 _assemble_final_chunks 가 일괄 수행한다(예외/미배선
                                # 경로에서도 동일 조립이 돌도록 — 토큰 캡은 항상 적용).
                                merged = sorted(
                                    chunks_by_id.values(),
                                    key=lambda c: c.score,
                                    reverse=True,
                                )
                except Exception:  # noqa: BLE001 — ToolUnknown 등 graceful skip
                    pass
                # phase 결과를 span 에 실어 Phoenix 에서 단계별 귀인 — 추출된 재검색 쿼리
                # 수 / 2차 검색 실행 수 / 컨텍스트에 새로 채택된 청크 수. output 은 생성된
                # 재검색 쿼리 요약(없으면 미설정).
                fs.set_attribute("follow_up.num_queries", len(fq_list))
                fs.set_attribute("follow_up.searchable_queries", searchable_count)
                fs.set_attribute("follow_up.added_chunks", follow_up_added)
                if fq_summary:
                    oi.set_io(fs, output_value=fq_summary)
            await emit_step("follow_up_search", "ok", added_chunks=follow_up_added)

            # === 최종 조립 — 1차 전량 보존 + 2차 score 순 채움(토큰 예산 캡) =======
            # follow_up 성공/실패/미배선 무관하게 항상 실행한다. budget=0 이면 캡 없이
            # (1차+2차 전량), >0 이면 추정 토큰 합이 예산을 넘는 만큼 2차 tail(낮은
            # score)부터 drop. 1차 청크는 1차만으로 예산을 초과할 때만 최후로 drop.
            chunks, budget_log, total_tokens_est, first_pass_dropped = (
                _assemble_final_chunks(
                    first_pass_ids, merged, self._context_token_budget
                )
            )
            evidence_gap = not chunks
            if budget_log:
                await emit_step("context_budget", "ok",
                                budget=self._context_token_budget,
                                total_tokens_est=total_tokens_est,
                                dropped=len(budget_log),
                                first_pass_dropped=first_pass_dropped)

            # 루프(tool 프레임)가 모두 끝난 *뒤* reasoning 을 단 한 번 방출 → 다음에 오는
            # 것은 context_build(step, 사이드채널) · generation 본문 토큰뿐이라 Thought
            # 블록과 본문이 연속된다(정상 렌더되는 `**질의 분류**` 패턴과 동일).
            if fq_summary:
                await emit_reasoning(f"\n**참조 문서 재검색**\n{fq_summary}\n")

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
                             else ("boost" if q.target.get("collection") else "none"),
                             # 스코프 채널별 (값, mode) 요약 + 무시/기각 감사(원칙 5·6 —
                             # silent 동작 금지). status↔규제 / design↔NuScale 배타성
                             # 위반·canonical_id 게이트 기각이 _dropped/_rejected 로 가시화.
                             "scope": _scope_summary(q)}
                            for q in queries
                        ],
                    },
                    "retrieval": {
                        "num_chunks": len(chunks),
                        "merged": len(merged),
                        "budget": self._max_context_chunks,
                        "fetch_k": per_query_k,
                        # 1차 검색 청크 수 — 최종 컨텍스트에 전량 반영됨(예산 안전판이
                        # 발동해 first_pass_dropped=True 가 아닌 한). num_chunks 와 대조.
                        "first_pass_kept": len(first_pass_ids),
                        "per_query_counts": per_query_counts,
                        "min_token_count": self._min_token_count,
                        "filters": dict(_NOISE_FILTER),
                        "floored_slots": coverage["floored_slots"],
                        "covered_required_slots": coverage["covered_required"],
                        "uncovered_required_slots": coverage["uncovered_required"],
                    },
                    "follow_up": {
                        # num_queries==0 ⇒ 2차 검색 스킵(미배선/실패) 또는 후속쿼리 없음.
                        # 키 자체는 항상 존재(스키마 균일) — 값으로 구별한다.
                        "num_queries": len(fq_list),
                        "added_chunks": follow_up_added,
                        "queries": [
                            {
                                "query_text": fq.get("query_text"),
                                "target_source_ids": fq.get("target_source_ids", []),
                                "intent": fq.get("intent"),
                            }
                            for fq in fq_list
                        ],
                    },
                    # 토큰 예산 거버너 결과(원칙 5/6 — silent cap 금지). budget=0 이면
                    # drop 없음. first_pass_dropped=True 는 1차 근거가 윈도우 안전판에
                    # 밀린 비정상 신호(감사 가시).
                    "context_budget": {
                        "budget": self._context_token_budget,
                        "total_tokens_est": total_tokens_est,
                        "dropped_chunk_ids": budget_log,
                        "first_pass_dropped": first_pass_dropped,
                    },
                    "evidence_gap": evidence_gap,
                    "session": self._session_pin(sess, post),
                }
            }

            # 사후 게이트 통과 시 conversation_summary(맥락) + session memory_ref 를 N4 에
            # 싣는다(memory ≠ evidence — summary 는 # CONVERSATION_SUMMARY, 청크와 분리).
            inject = post.inject and bool(sess["state"])
            convo_summary = (
                (sess["state"] or {}).get("running_summary") or None
            ) if inject else None
            memory_refs: tuple[MemoryRef, ...] = ()
            memory_ids_used: list[str] = []
            if inject and request.session_id:
                memory_ids_used.append(request.session_id)
                memory_refs = (
                    MemoryRef(
                        memory_id=request.session_id, memory_type="session",
                        review_status=MemoryReviewStatus.APPROVED.value,
                        staleness_status=StalenessStatus.FRESH.value,
                    ),
                )

            # === N4 Generation ===========================================
            await emit_step("context_build", "started")
            with _TRACER.start_as_current_span("agent.context_build") as s:
                pack = self._context_builder.build(
                    interaction_id=request.interaction_id,
                    query_text=request.query_text,
                    chat_history=request.chat_history if inject else (),
                    conversation_summary=convo_summary,
                    scenario_object="n_a", scenario_depth="n_a",
                    entities={}, chunks=chunks, memory_refs=memory_refs,
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

            # === N5 Session Update (멀티턴 — 누적은 도구 내부) ============
            # explicit_references 를 누적 참조로, follow-up source_id·chunk_id 를 검색
            # 이력으로, governing_class/route 를 variant_state 로 싣는다. topic_signature
            # 는 N1 topic_label sha16(없으면 결정론 fallback).
            await self._session_update(
                request, ctx, record,
                user_turn=request.query_text, assistant_turn=answer_text,
                references=list(spec.explicit_references),
                chunk_ids=chunk_ids, source_ids=_source_ids_of(chunks, fq_list),
                topic_signature=_topic_signature(spec),
                memory_ids_used=memory_ids_used,
                variant_state={
                    "governing_normative_class": spec.governing_normative_class or "",
                    "route": triage.route,
                    "intent": spec.intent,
                },
                prior_summary=(sess["state"] or {}).get("running_summary"),
            )

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
                    memory_ids_used=tuple(memory_ids_used),
                    memory_types_used=tuple("session" for _ in memory_ids_used),
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
                           llm_id: str, triage_pin: dict[str, Any],
                           ctx: ToolExecutionContext | None = None,
                           sess: dict[str, Any] | None = None,
                           record=None) -> AgentResponse:
        metrics = get_metrics()
        sess = sess or {"enabled": False, "state": None, "pre_inject": False,
                        "load_present": False, "pre_reason": "disabled"}
        qu_pin: dict[str, Any] = {
            "spec_driven": {
                "route": "general", "triage": triage_pin,
                # general 은 주입 안 함(자기완결 추론) — 사후 게이트도 미적용.
                "session": self._session_pin(
                    sess, SessionInjectionDecision(False, "general_route")
                ),
            }
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
            query_understanding=qu_pin, node="general",
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

        # N5 — general 턴도 대화/turn_count 를 누적한다(route=general 기록, 참조는 비어
        # 있을 수 있음). ctx/record 가 없으면(직접 호출/테스트) skip.
        if ctx is not None and record is not None:
            await self._session_update(
                request, ctx, record,
                user_turn=request.query_text, assistant_turn=answer_text,
                references=[], chunk_ids=[], source_ids=[],
                topic_signature=None, memory_ids_used=[],
                variant_state={"route": "general"},
                prior_summary=(sess.get("state") or {}).get("running_summary"),
            )

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

    # ------------------------------------------------------------------
    # 멀티턴 세션 메모리 — N-1 load / 사전·사후 게이트 / N5 update / 재현 핀.
    # 설계: docs/plans/spec_driven_session_memory.design.v1.md.
    # ------------------------------------------------------------------
    async def _session_load(self, request: AgentRequest,
                            ctx: ToolExecutionContext, record) -> dict[str, Any]:
        """N-1 — session_load + 사전 게이트(history/variant_switch). 비활성/세션ID 부재/
        미배선·실패 시 graceful(pre_inject=False)."""
        out: dict[str, Any] = {
            "enabled": self._session_enabled, "state": None,
            "load_present": False, "pre_inject": False, "pre_reason": "disabled",
        }
        if not self._session_enabled or not request.session_id:
            out["pre_reason"] = "disabled" if not self._session_enabled else "no_session_id"
            return out
        try:
            res = await self._tools.invoke(
                "memory.session_load", {"session_id": request.session_id}, ctx,
            )
            record(res)
            if res.output and res.output.get("present"):
                out["state"] = dict(res.output)
                out["load_present"] = True
        except Exception:  # noqa: BLE001 — ToolUnknown 등 graceful skip(단일턴 degrade).
            return out
        # 사전 게이트 — current route/authority/topic 미확정이라 history/variant_switch 만.
        if not request.chat_history:
            out["pre_reason"] = "no_history"
            return out
        if not out["load_present"]:
            out["pre_reason"] = "no_prior_state"
            return out
        prior_variant = (out["state"] or {}).get("last_variant_id")
        if prior_variant and prior_variant != self.spec.variant_id:
            out["pre_reason"] = "variant_switch"
            return out
        out["pre_inject"] = True
        out["pre_reason"] = "follow_up"
        return out

    def _build_prior_context(self, sess: dict[str, Any]) -> str | None:
        """사전 게이트 통과 시 N0/N1 에 동반할 PRIOR CONTEXT 블록(요약 + 상위 salience
        참조). anaphora 해소용 *맥락*이지 evidence 가 아니다(프롬프트가 명시)."""
        state = sess.get("state") or {}
        summary = (state.get("running_summary") or "").strip()
        refs = [r.get("ref_id") for r in state.get("tracked_references", [])
                if r.get("ref_id")][:8]
        if not summary and not refs:
            return None
        lines = ["# PRIOR CONTEXT (직전 대화 — 질의의 지시표현 해소용, 근거 아님)"]
        if summary:
            lines.append(summary)
        if refs:
            lines.append("이전 참조: " + ", ".join(refs))
        return "\n".join(lines)

    def _post_gate(self, request: AgentRequest, sess: dict[str, Any],
                   triage, spec: AnswerSpec) -> SessionInjectionDecision:
        """N1.5 — 사후 게이트(route/authority/topic/ref overlap 전부). 비활성/미적재면
        주입 안 함."""
        if not sess.get("enabled") or not sess.get("load_present"):
            return SessionInjectionDecision(False, sess.get("pre_reason", "disabled"))
        state = sess.get("state") or {}
        vstate = (state.get("variant_state") or {}).get(self.spec.variant_id, {})
        prior_variant = state.get("last_variant_id")
        return decide_session_injection(
            has_history=bool(request.chat_history),
            variant_switched=bool(prior_variant
                                  and prior_variant != self.spec.variant_id),
            current_topic_signature=_topic_signature(spec),
            prior_topic_signature=state.get("topic_signature"),
            prior_references=[r.get("ref_id")
                              for r in state.get("tracked_references", [])
                              if r.get("ref_id")],
            current_references=list(spec.explicit_references),
            continuity_signals={
                "route": (vstate.get("route"), triage.route),
                "authority": (vstate.get("governing_normative_class"),
                              spec.governing_normative_class),
            },
            overlap_threshold=self._session_overlap_threshold,
        )

    def _session_pin(self, sess: dict[str, Any],
                     post: SessionInjectionDecision) -> dict[str, Any]:
        """재현 핀(원칙 5/6 — silent 동작 금지) — pre/post 게이트·참조·신호를 기록."""
        state = sess.get("state") or {}
        prior_refs = [r.get("ref_id") for r in state.get("tracked_references", [])
                      if r.get("ref_id")]
        return {
            "enabled": sess.get("enabled", False),
            "session_id_present": bool(state) or sess.get("load_present", False),
            "loaded": sess.get("load_present", False),
            "turn_count": state.get("turn_count", 0),
            "pre_gate": {"inject": sess.get("pre_inject", False),
                         "reason": sess.get("pre_reason", "disabled")},
            "post_gate": {"inject": post.inject, "reason": post.reason,
                          "matched_references": post.matched_references},
            "prior_references": prior_refs,
            "prior_topic": state.get("topic_signature"),
        }

    async def _session_update(self, request: AgentRequest,
                              ctx: ToolExecutionContext, record, *,
                              user_turn: str, assistant_turn: str,
                              references: list[str], chunk_ids: list[str],
                              source_ids: list[str], topic_signature: str | None,
                              memory_ids_used: list[str],
                              variant_state: dict[str, Any],
                              prior_summary: str | None = None) -> None:
        """N5 — session_update(누적은 도구 내부). 비활성/세션ID 부재/미배선 graceful skip.
        running_summary 는 summarizer 가 있고 keep_turns 초과 시에만 압축(없으면 None →
        도구가 prior 보존)."""
        if not self._session_enabled or not request.session_id:
            return
        running_summary: str | None = None
        if self._summarizer is not None:
            try:
                summ = await self._summarizer.summarize(
                    prior_summary=prior_summary,
                    chat_history=request.chat_history,
                )
                # compressed=False(윈도우 내)면 prior 보존 의미로 None(도구가 미갱신).
                running_summary = summ.summary if summ.compressed else None
            except Exception:  # noqa: BLE001 — 요약 실패는 미갱신(prior 보존).
                running_summary = None
        try:
            res = await self._tools.invoke(
                "memory.session_update",
                {
                    "session_id": request.session_id,
                    "variant_id": self.spec.variant_id,
                    "user_turn": user_turn,
                    "assistant_turn": assistant_turn,
                    "new_references": [{"ref_id": r, "ref_type": "regulation"}
                                       for r in references],
                    "retrieved_chunk_ids": chunk_ids,
                    "retrieved_source_ids": source_ids,
                    "running_summary": running_summary,
                    "topic_signature": topic_signature,
                    "memory_ids_used": memory_ids_used,
                    "variant_state": variant_state,
                    "keep_turns": self._session_keep_turns,
                    "retrieval_window": self._session_retrieval_window,
                },
                ctx,
            )
            record(res)
        except Exception:  # noqa: BLE001 — ToolUnknown/실패 graceful(상태 미갱신).
            pass

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
        parts.append("# ANSWER SPEC\n" + _render_spec_block(spec))
        parts.append("# CONTEXT\n" + context_block)
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
                        query_understanding: dict[str, Any] | None = None,
                        node: str = "generation"):
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
            except LLMUnavailableError as exc:
                s.set_attribute("llm.status", "unavailable")
                # upstream(외부 LLM) 원인을 span 에 남긴다(v3_1 분류기와 동형) — 내부
                # 원인은 trace 에만, 사용자 응답엔 싣지 않는다(원칙 6). 어댑터가 실은
                # 메시지("upstream 4xx: ...", httpx 예외 등)가 그대로 닿는다.
                s.record_exception(exc)
                s.set_attribute("llm.upstream_error", str(exc)[:500])
                # 구조화 로그(structlog → stdout JSON + OTLP→Loki). _add_trace_context 가
                # trace_id/span_id 를 실어 Loki↔Tempo 점프가 가능하고, 외부 요소
                # (LLM 엔드포인트 4xx/5xx/타임아웃/연결거부)를 추적한다. getattr 는
                # llm 어댑터 종류 무관(fake adapter 는 model_id 없음) 안전 접근.
                _LOG.warning(
                    "llm_unavailable",
                    node=node,
                    interaction_id=request.interaction_id,
                    variant=self.spec.variant_id,
                    model_id=getattr(llm, "model_id", "unknown"),
                    upstream_error=str(exc)[:500],
                    error_type=type(exc).__name__,
                )
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
        "required_slots (facet = the *kind* of evidence — develop each per its facet):",
    ]
    for s in spec.required_slots:
        # facet 라벨을 N4 에 노출 — 생성기가 슬롯별로 어떤 표현축(Axis 1~3)을 적용할지
        # 안다(definition→정의 layer, quantitative_limit→값+기술근거, review_finding→
        # 판단/조건 분리). 결정=코드(결정론 렌더), 표현=모델(답변 심도 §3.2/P3).
        tag = f" [{s.facet}]" if s.facet else ""
        flag = "" if s.required else " (supporting)"
        lines.append(f"- {s.name}{tag}{flag}: {s.description}".rstrip())
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


# char→token 휴리스틱(한/영 혼합). vLLM 윈도우 안전 쪽으로 기울도록 과대추정 편향
# (작은 divisor). token_count(전체 청크)는 생성 프롬프트 기여분과 척도가 달라 쓰지
# 않고 본문 길이로 추정한다.
_CHARS_PER_TOKEN = 3
_CHUNK_HEADER_OVERHEAD = 12  # 인용 헤더 라인([cite-N] doc#chunk (p=..)) 토큰 근사.


def _estimate_chunk_tokens(chunk: RetrievedChunk) -> int:
    # full 모드 render 가 본문으로 text(전문)를 쓰므로 추정도 text 우선으로 일치시킨다
    # (snippet 우선 시 전문 길이를 과소추정해 예산 거버너가 윈도우 초과를 놓침 — D6).
    # 표 치환 *전* 길이라 표 본문이 마커보다 길면 약간 과소추정하나, 위 과대추정
    # 편향이 일부 상쇄한다.
    body = chunk.text or chunk.snippet or ""
    return max(1, len(body) // _CHARS_PER_TOKEN) + _CHUNK_HEADER_OVERHEAD


def _assemble_final_chunks(
    first_pass_ids: set[str],
    merged: list[RetrievedChunk],
    token_budget: int,
) -> tuple[list[RetrievedChunk], list[str], int, bool]:
    """최종 N4 컨텍스트 조립(설계: 1차 전량 + 2차 score 순, 토큰 예산 캡).

    `merged` 는 1차+2차 통합 score desc. `first_pass_ids` 는 1차 검색 청크.

    Phase A — 1차 청크 전량 포함(merged 의 score desc 순서 유지).
    Phase B — 2차 청크(id ∉ first_pass_ids)를 score desc 로 append.
    Phase C — token_budget>0 이면 Σ추정토큰이 예산 이하가 되도록 **2차 tail(낮은
      score)부터** drop. 2차를 다 버려도 초과하면 1차 tail 을 drop 하고
      first_pass_dropped=True(윈도우 안전판 — 비정상 신호).

    반환: (chunks[score desc], budget_log, total_tokens_est, first_pass_dropped).
    budget_log 는 drop 된 chunk_id 리스트(silent cap 금지 — 원칙 6).
    """
    first = [c for c in merged if c.chunk_id in first_pass_ids]
    second = [c for c in merged if c.chunk_id not in first_pass_ids]
    chunks = first + second
    budget_log: list[str] = []
    first_pass_dropped = False

    total = sum(_estimate_chunk_tokens(c) for c in chunks)
    if token_budget > 0 and total > token_budget:
        # 2차 tail 부터 drop(가장 낮은 score = 리스트 끝).
        while total > token_budget and second:
            dropped = second.pop()
            chunks.remove(dropped)
            total -= _estimate_chunk_tokens(dropped)
            budget_log.append(dropped.chunk_id)
        # 2차를 다 버려도 초과 → 1차 tail drop(최후 안전판).
        while total > token_budget and len(first) > 1:
            dropped = first.pop()
            chunks.remove(dropped)
            total -= _estimate_chunk_tokens(dropped)
            budget_log.append(dropped.chunk_id)
            first_pass_dropped = True

    return chunks, budget_log, total, first_pass_dropped


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
        max_context_chunks=t.get("spec_driven_max_context_chunks", 20),
        min_token_count=t.get("retriever_min_token_count", 0),
        context_token_budget=t.get("spec_driven_context_token_budget", 0),
        follow_up_fetch_k=t.get("spec_driven_follow_up_fetch_k", 8),
        follow_up_keep_k=t.get("spec_driven_follow_up_keep_k", 3),
        summarizer=deps.summarizer,
        session_memory_enabled=t.get("spec_driven_session_memory_enabled", False),
        session_keep_turns=t.get("spec_driven_session_keep_turns", 10),
        session_retrieval_window=t.get("spec_driven_session_retrieval_window", 5),
        session_overlap_threshold=t.get("spec_driven_session_overlap_threshold", 0.5),
    )
