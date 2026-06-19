"""composer_base — composer/composer_pipelined 가 공유하는 base 러너 + 모듈 헬퍼.

이전엔 이 코드가 `spec_driven_v1.py`(SpecDrivenRunner) 안에 살았고 composer 계열이 그
variant 클래스를 상속했다. spec_driven_v1/v2 variant 를 폐기하면서, composer 가 의존하던
**공유 인프라**(검색 앞단 세션/게이트/프롬프트/생성/거부 헬퍼 + 모듈 함수)를 여기로 추출해
composer 계열이 자족(自足)하게 한다. spec_driven_v1 의 `run()`(4-노드 선형 conductor)·
`_post_retrieval`(follow-up 2차 검색 시임)·variant 등록은 *composer 가 쓰지 않으므로* 옮기지
않는다 — composer 는 `run()` 을 슬롯 파이프라인으로 완전 오버라이드한다.

`ComposerBase` 가 보유하는 것(composer/composer_pipelined 가 상속·호출):
  - `__init__` — 25개 생성자 파라미터 → 인스턴스 속성(슬롯 파이프라인 mixin contract 포함:
    `_tools`/`_min_token_count`/`_follow_up_fetch_k`/`_follow_up_keep_k`).
  - `run_stream` — API(openai_compat)가 호출하는 스트리밍 진입점(composer 는 `run` 만
    오버라이드하므로 이 wrapper 를 상속해 그대로 쓴다).
  - `_run_general` — N0 route=general 직답 경로(검색·도구·인용 없음).
  - 세션 멀티턴 헬퍼 — `_session_load`/`_build_prior_context`/`_post_gate`/`_session_pin`/
    `_session_update`.
  - 프롬프트 렌더 — `_render_general_prompt`/`_render_generation_prompt`(gap-answer 단일 경로).
  - 생성/거부 — `_generate`/`_generate_stream`/`_refuse`.

모듈 함수/상수(composer/composer_pipelined/slot_pipeline 가 import): `_NOISE_FILTER`,
`_SEARCH_TOOL`, `_sha16`, `_topic_signature`, `_scope_summary`, `_source_ids_of`,
`_render_spec_block`, `_parse_chunks`, `_select_with_slot_floor`, `_assemble_final_chunks`,
`_to_citations`.

원칙(CLAUDE.md): 재현성(핀·해시)·통제된 도구·실패 1급 outcome 불변. 동작은 추출 전과
동형이다(net-neutral) — composer 회귀 0 이 목표."""

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
from app.application.agents.llm_router import LLMRouter
from app.application.context.pack import ContextBuilder, ContextPack
from app.application.events.recorder import EventRecorder
from app.application.memory.policies import (
    SessionInjectionDecision,
    decide_session_injection,
)
from app.domain.agents import VariantSpec
from app.domain.errors import RefusalReason, VerificationStatus
from app.domain.interaction import AgentRequest, AgentResponse, Citation, ToolCallRecord
from app.domain.retrieval import RetrievedChunk
from app.domain.spec_driven import AnswerSpec, FormulatedQuery
from app.observability import openinference as oi
from app.observability.logging import get_logger
from app.observability.metrics import get_metrics
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.llm import LLMPort, LLMResult, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")
_LOG = get_logger("agent.composer_base")

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
        _PAGE_RANGE_FIELD,
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
        # 10CFR Part→page 좁힘(설계 §4) — filter 일 때만 실린다(page_range 는 hard-scope).
        "page_range": _ch(_PAGE_RANGE_FIELD),
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


class ComposerBase:
    """composer/composer_pipelined 공유 base — 검색 앞단 세션/게이트/프롬프트/생성/거부
    헬퍼와 streaming 진입점을 보유한다. 서브클래스(`ComposerRunner`)가 `run()` 을 슬롯
    파이프라인으로 오버라이드하고, 이 base 의 `run_stream`(API 진입)·`_run_general`·세션·
    프롬프트·생성·거부 헬퍼를 상속해 재사용한다.

    근거 0건이면 거부 대신 **gap-answer**(사용자 결정)하되, 사전 지식으로 규제 사실을
    지어내지 못하게 N4 프롬프트가 parametric 답변을 hard-forbid 한다(CLAUDE.md #6 호환).
    재현성·통제된 도구·실패 1급 불변식은 유지한다."""

    # N4 / N4-G 생성 프롬프트의 event/step profile_id 라벨. prompt_body 자체는 주입된
    # source(registry 호스팅)가 소유하나, 이벤트·스텝에 찍히는 profile_id 문자열은 변형마다
    # 다르다(재현 핀이 어느 프롬프트 정책으로 생성됐는지 단독 설명 — 원칙 5). 서브클래스가
    # 필요 시 `*_v1` 외로 오버라이드한다. 동작 불변(기본값=기존 하드코딩 문자열).
    _GENERATION_PROFILE_ID = "spec_driven_generation_v1"
    _GENERAL_PROFILE_ID = "spec_driven_general_v1"

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
    # Streaming wrapper — react_minimal/v4 와 동일 패턴(검증됨). API(openai_compat)가
    # 호출하는 진입점. 서브클래스가 `run` 만 오버라이드하므로 이 wrapper 는 상속된다.
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

    async def run(self, request: AgentRequest) -> AgentResponse:
        """서브클래스(`ComposerRunner`)가 슬롯 파이프라인으로 오버라이드한다. base 자체는
        진입점이 아니므로 미구현(직접 호출 금지 — run_stream→run 은 서브클래스 run 을 탄다)."""
        raise NotImplementedError(
            "ComposerBase.run() is abstract — ComposerRunner overrides it"
        )

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
                        profile_id=self._GENERAL_PROFILE_ID,
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
                prompt_profile_id=self._GENERAL_PROFILE_ID,
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
    # composer 의 gap-answer/슬롯없음 단일 경로(_generate_single)가 호출한다.
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
    # 거부 — 1급 outcome(원칙 6). composer 계열은 LLM_UNAVAILABLE 만 거부한다
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
        # 안다(definition→정의 layer, technical_basis→값+출처/보수성, review_finding↔
        # applicant_design 주장/판단 분리). 결정=코드(결정론 렌더), 표현=모델(expert_grade §2/P3).
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
            source_url=c.source_url, tables=c.tables,
            kind=c.kind, table_tag=c.table_tag,
        )
        for c in candidates
    )


def _refusal_message(reason: RefusalReason) -> str:
    if reason is RefusalReason.LLM_UNAVAILABLE:
        return "응답이 지연되거나 모델을 가져올 수 없습니다. 잠시 후 다시 시도해 주세요."
    return "답변을 제공할 수 없습니다."
