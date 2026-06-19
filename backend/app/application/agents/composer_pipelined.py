"""composer_pipelined — 슬롯 검색-생성 파이프라이닝(배리어 제거) Agent.

설계: docs/plans/spec_driven_slot_pipeline_streaming.design.v1.md.

두 변형의 절반씩을 결합한다:
  - **spec_driven_v2** 의 슬롯별 검색-검증 파이프라인(`_SlotPipelineMixin._run_slot_pipeline`
    — Node1 검증 → Node2 외부참조 선별 → 2차 검색 → 재검증).
  - **composer** 의 N4 슬롯 단위 순차 생성 + 즉시 토큰 스트리밍(`ComposerRunner` 의 슬롯
    프롬프트·검수·종합 머신).

핵심은 **배리어 제거**다. composer 는 N3/N3.5 검색을 *전부* 끝낸 뒤에야 첫 슬롯을 생성하나,
composer_pipelined 는 N2 직후 슬롯별 검색-검증 task 를 *즉시* 발사하고(`SlotSearchHandle`),
생성 루프가 슬롯 i 직전 그 슬롯 future 만 `await` 한다 — 나머지 슬롯 검색은 백그라운드 진행
(검색 대기가 생성 뒤로 숨음). slot i 가 준비되는 즉시(다른 슬롯 대기 없이) slot i 본문을
스트리밍한다.

**cite-N(전역 배정 — `SlotCitationAllocator`):** 슬롯 CONTEXT 로 넘어가기 *전*(프롬프트 렌더
전)에 그 슬롯 청크에 **전역 단일 공간**의 cite-N 을 배정한다. 모델은 처음부터 올바른 전역
[cite-N] 으로 생성하므로 스트리밍 토큰이 곧 최종 번호다(사후 재번호 없음 → 서로 다른 근거가
같은 [cite-0] 으로 보이던 문제 제거). 생성은 순차라 슬롯 소비 순서대로 번호가 늘어 결정적이고,
검색은 여전히 병렬(번호 배정은 *생성* 시점이라 검색 병렬성 무관). 슬롯 간 공유 chunk 는 최초
등장 슬롯의 cite 를 재사용해 References 가 단일화된다(전체 청크 확정 불요 = 완전 배리어 제거).

**결정성(CLAUDE.md #5):** future 완료 순서는 비결정적이나, 소비·record·통합은 전부 생성
루프의 슬롯 *생성 순서*(depends_on 위상정렬)로 한다(v1 idiom). 같은 입력이면 tool_calls
순서·해시·답이 future 완료 순서와 무관하게 동일하다.

spec_driven_v1/v2/composer 는 불변 보존(A/B·회귀 control)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.application.agents.events import emit_reasoning, emit_step, emit_token
from app.application.agents.composer import ComposerRunner
from app.application.agents.registry import AgentDeps, register_variant
from app.application.agents.composer_base import (
    _render_spec_block,
    _sha16,
    _source_ids_of,
    _to_citations,
)
from app.application.agents.slot_pipeline import (
    SlotCitationAllocator,
    SlotSearchHandle,
    SlotSearchResult,
    _SlotPipelineMixin,
    _SlotPipelineResult,
)
from app.application.intake.spec_driven_answer_spec import (
    SpecDrivenAnswerSpecInstantiator,
)
from app.application.intake.spec_driven_query import QueryFormulator
from app.application.intake.spec_driven_triage import SpecDrivenTriage
from app.domain.agents import VariantSpec
from app.domain.errors import RefusalReason, VerificationStatus
from app.domain.interaction import AgentRequest, AgentResponse, ToolCallRecord
from app.domain.memory import MemoryRef, MemoryReviewStatus, StalenessStatus
from app.domain.retrieval import RetrievedChunk
from app.domain.spec_driven import PERSONAS, AnswerSpec, Persona, SpecSlot
from app.observability import openinference as oi
from app.observability.logging import get_logger
from app.observability.metrics import get_metrics
from app.observability.otel import get_tracer
from app.ports.llm import LLMPort, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")
_LOG = get_logger("agent.composer_pipelined")

COMPOSER_PIPELINED_VARIANT_ID = "composer_pipelined"

# 슬롯 단위 환각 검증 — **LLM-as-judge**(composer_orchestrated_generation.design.v2 §1, 사용자
# 결정). 결정론 등급 게이트(cite-범위·약귀속 룰)는 신뢰할 수 없어 폐기하고, 외부 verifier 모델이
# 슬롯 출력 ↔ CONTEXT entailment 를 판정(verdict)하고 그 *이유*(rationale)를 직접 설명한다
# (feedback_model_over_rule: 표현·판정=모델). 환각이 없으면(supported) pass — 표시 없음.
#
# verdict → OpenWebUI alert 종류(presentation-only lookup — 판정이 아니라 *렌더 매핑*. OpenWebUI
# marked+AlertRenderer.svelte 가 렌더하는 GitHub alert 5종 NOTE/TIP/IMPORTANT/WARNING/CAUTION
# 중에서 고른다 — 미렌더 `[!DANGER]` 금지). alert *본문*은 정적 템플릿이 아니라 모델 rationale.
_VERDICT_SUPPORTED = "supported"   # 환각 없음 → pass(무표시).
_VERDICT_PARTIAL = "partial"       # 부분 입증 → [!CAUTION].
_VERDICT_UNSUPPORTED = "unsupported"  # 근거 미입증 → [!WARNING].
# 판정 자체가 안 돈 경우(verifier 미배선/실패) — 결정론 룰로 대체하지 않는다(사용자 결정:
# 결정론 신뢰 불가). 검증 불가를 솔직히 표시([!NOTE])하되 본문은 그대로 둔다.
_VERDICT_SKIPPED = "skipped"

_VERDICT_ALERT = {
    _VERDICT_PARTIAL: "CAUTION",
    _VERDICT_UNSUPPORTED: "WARNING",
    _VERDICT_SKIPPED: "NOTE",
}


class ComposerPipelinedRunner(_SlotPipelineMixin, ComposerRunner):
    """composer_pipelined 러너 — `_SlotPipelineMixin`(v2 4-stage 검색-검증) + `ComposerRunner`
    (슬롯 생성/검수/종합)를 결합한다. `run()` 만 새로 짜서 N2 직후 슬롯 검색 future 를 발사하고
    생성 루프가 슬롯별로 소비한다. 슬롯 프롬프트·헤더·검수·종합·prior_sections·`_finalize_turn`
    은 ComposerRunner 그대로 재사용한다(생성 토폴로지 불변 — 파이프라이닝은 *지연* 만 바꾼다)."""

    _GENERATION_PROFILE_ID = "spec_driven_generation_pipelined_v1"
    _GENERAL_PROFILE_ID = "spec_driven_general_pipelined_v1"

    def __init__(self, *args: Any, verify_concurrency: int = 10,
                 relevance_llm_id: str = "", multihop_llm_id: str = "",
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 동시 슬롯 검색-검증 상한(v2 와 동형). 검증 도구 자체 semaphore 와 함께 vLLM 보호.
        self._verify_sem = asyncio.Semaphore(max(1, verify_concurrency))
        # 노드 분리 가시화(원칙 5/6) — relevance(verify_slot)=utility_llm, multihop(follow_up)=
        # secondary_llm 이 *어느 pool id(엔드포인트)* 에서 돌았는지 재현 핀에 남긴다. 부트 핀
        # 이라 요청별 추종이 아님. 빈 값이면 핀에서 생략(미배선/단일노드).
        self._relevance_llm_id = relevance_llm_id
        self._multihop_llm_id = multihop_llm_id
        # generation 부모 span(agent.generation) turn-local 종료 콜백. _run_slot_generation_loop
        # 가 매 turn 핀하고 _generate_pipelined finally 가 안전 종료한다(미설정=no-op).
        self._end_generation_span = None

    # ------------------------------------------------------------------
    # run() — N0~N2 계승(composer 동형), N3 부터 슬롯 future 발사 + 파이프라인 생성.
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
            from app.application.agents.events import emit_tool_nowait
            emit_tool_nowait(
                r.tool_name, r.status, version=r.tool_version,
                latency_ms=r.latency_ms, error_code=r.error_code,
                retry_count=r.retry_count,
            )

        if self._answer_spec_source is None or self._query_source is None \
                or self._generation_source is None \
                or self._triage_source is None or self._general_source is None:
            raise RuntimeError(
                "composer_pipelined prompt sources not wired — N0/N1/N2/N4/N4-G prompts "
                "are registry-hosted (prompts/registry.yaml spec_driven_* blocks)"
            )

        from app.application.agents.llm_router import UnknownLLMError
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

            # === N-1 Session Load + 사전 게이트(계승) ======================
            sess = await self._session_load(request, ctx, record)
            prior_context = (
                self._build_prior_context(sess) if sess["pre_inject"] else None
            )

            # === N0 Triage(계승) =========================================
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
            if triage.rationale:
                await emit_reasoning(f"\n**질의 분류**\n{triage.rationale}\n")

            # general 분기(계승) — 슬롯 분해 비대상.
            if triage.route == "general":
                return await self._run_general(
                    request, started, tool_calls, llm=llm, llm_id=llm_id,
                    triage_pin=triage_pin, ctx=ctx, sess=sess, record=record,
                )

            # === N1 Define Spec(계승) ====================================
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
                                        prior_context=prior_context,
                                        persona_profile=self._persona_profile())
            await emit_step("define_spec", "ok", method=spec.instantiation_method,
                            num_slots=len(spec.required_slots),
                            num_refs=len(spec.explicit_references))

            post = self._post_gate(request, sess, triage, spec)

            # === N2 Query Formulation(계승) ==============================
            await emit_step("query_formulation", "started")
            n2 = QueryFormulator(
                util,
                prompt_body=self._query_source.prompt_body,
                schema=self._query_source.schema or None,
                model_options=self._query_source.model_options or None,
                policy_hash=self._query_source.policy_hash,
            )
            queries, formulation_method = await n2.formulate(
                request.query_text, spec, reasoning_label="검색 쿼리 생성",
                persona_profile=self._persona_profile())
            truncated = False
            if len(queries) > self._max_queries:
                truncated = True
                queries = queries[: self._max_queries]
            await emit_step("query_formulation", "ok", method=formulation_method,
                            num_queries=len(queries), truncated=truncated)

            # === N3+N3.5 — 슬롯별 검색-검증 future 발사(배리어 없음, §2/§3) =========
            # N2 직후 슬롯별 task 를 즉시 띄운다. 생성 루프가 슬롯 i 직전 그 future 만 await
            # 하고, 나머지 슬롯 검색은 백그라운드 진행(검색 대기가 생성 뒤로 숨음). 검색·검증
            # tool_results 는 task 안에서 record 하지 않고 결과에 실어 두고, 생성 루프가 슬롯
            # 소비 시점에 순차 record 한다(결정성·순서·race — §5.1/§6).
            await emit_step("slot_search", "started", num_slots=len(spec.required_slots))
            handle = self._fire_slot_searches(
                request=request, ctx=ctx, spec=spec, queries=queries)

            # === N4 — 파이프라인 슬롯 생성(또는 검색 0건 시 gap 경로) =================
            return await self._generate_pipelined(
                request, started, tool_calls, tool_result_refs,
                llm=llm, llm_id=llm_id, ctx=ctx, record=record,
                spec=spec, triage=triage, handle=handle,
                triage_pin=triage_pin, formulation_method=formulation_method,
                queries=queries, truncated=truncated,
                sess=sess, post=post, root=root,
            )

    # ------------------------------------------------------------------
    # N3 슬롯 검색 발사 — 슬롯별 검색-검증 task 를 즉시 띄운다(SlotSearchHandle).
    # ------------------------------------------------------------------
    def _fire_slot_searches(
        self, *, request: AgentRequest, ctx: ToolExecutionContext,
        spec: AnswerSpec, queries,
    ) -> SlotSearchHandle:
        """슬롯명 → 검색-검증 task. 슬롯별로 그 슬롯에 귀속된 N2 쿼리들만 1차 검색하고
        (`_slot_first_pass_search`), 이어 4-stage 검증 파이프라인(`_run_slot_pipeline`)을
        돈다. 배리어 없이 동시 발사 — gather 는 생성 루프가 슬롯별 await 로 대신한다."""
        spec_block = _render_spec_block(spec)
        per_query_k = max(self._top_k, self._max_context_chunks)
        # 슬롯별 N2 쿼리 그룹(슬롯명 일치). 슬롯에 쿼리가 없으면 원질의 1개로 폴백.
        queries_by_slot: dict[str, list[dict[str, Any]]] = {}
        for q in queries:
            queries_by_slot.setdefault(q.slot_name, []).append(
                {"query_text": q.query_text, "target": q.target, "filters": q.filters}
            )
        slot_query_text: dict[str, str] = {}
        for q in queries:
            slot_query_text.setdefault(q.slot_name, q.query_text)

        async def _search_slot(slot_name: str) -> SlotSearchResult:
            async with self._verify_sem:
                slot_queries = queries_by_slot.get(slot_name) or [
                    {"query_text": request.query_text, "target": {}, "filters": {}}
                ]
                first_pass, pre_results = await self._slot_first_pass_search(
                    ctx=ctx, slot_queries=slot_queries, per_query_k=per_query_k)
                res = await self._run_slot_pipeline(
                    request=request, ctx=ctx, spec_block=spec_block,
                    slot_name=slot_name,
                    slot_query=slot_query_text.get(slot_name, request.query_text),
                    slot_chunks=first_pass, pre_tool_results=pre_results,
                )
                return SlotSearchResult(
                    slot_name=slot_name, necessary=res.necessary,
                    second_pass=res.second_pass, neighbor=res.neighbor_chunks,
                    pipeline=res,
                )

        tasks: dict[str, asyncio.Task[SlotSearchResult]] = {
            s.name: asyncio.create_task(_search_slot(s.name))
            for s in spec.required_slots
        }
        return SlotSearchHandle(tasks)

    # ------------------------------------------------------------------
    # 슬롯 future 소비 — task 예외를 그 슬롯만 degrade(method="error")로 흡수(설계 §7).
    # ------------------------------------------------------------------
    async def _consume_slot_result(self, handle: SlotSearchHandle,
                                   slot_name: str) -> SlotSearchResult:
        """슬롯 검색-검증 future 를 소비한다. task 가 내부 예외를 던지면(검색·검증 전체
        실패) 전체 턴을 죽이는 대신(설계 §7 — "전체 실패 아님") *그 슬롯만* 빈 기여로
        degrade 한다: `method="error"` 핀 + structlog. 생성 루프는 빈 CONTEXT 로 그 슬롯을
        생성 시도하고 한계를 명시한다(composer fallback 계승). 정상 결과는 그대로 반환."""
        try:
            return await handle.result(slot_name)
        except Exception as exc:  # noqa: BLE001 — 슬롯 1개 실패를 1급으로 흡수(turn 보존).
            _LOG.warning(
                "slot_search_failed", node=f"slot:{slot_name}",
                variant=self.spec.variant_id,
                error_type=type(exc).__name__, error=str(exc)[:500],
            )
            return SlotSearchResult(
                slot_name=slot_name, necessary=[], second_pass=[],
                pipeline=_SlotPipelineResult(
                    slot_name=slot_name, method="error", num_first_pass=0,
                    necessary=[], multihop_ids=[],
                    rationale=f"⚠ 슬롯 검색-검증 실패({type(exc).__name__}) → 빈 CONTEXT 로 생성",
                ),
            )

    # ------------------------------------------------------------------
    # 슬롯 단위 환각 검증 — LLM-as-judge(design.v2 §1, 사용자 결정). 결정론 등급 게이트 폐기.
    # verifier 모델이 슬롯 출력 ↔ CONTEXT entailment 를 판정(verdict)하고 *그 이유*(rationale)를
    # 직접 설명한다. 생성과 분리된 별도 콜(self-verification 금지 — 외부 verifier). 환각 없으면
    # (supported) pass(무표시). alert *본문*은 정적 템플릿이 아니라 모델 rationale 이다.
    # ------------------------------------------------------------------
    async def _judge_slot(
        self, llm: LLMPort, text: str,
        sub_chunks: list[RetrievedChunk], pack,
    ) -> dict[str, Any]:
        """LLM judge 호출 → {verdict, rationale, unsupported_claims}. verifier source 미배선/
        실패면 verdict=skipped(결정론 룰로 대체하지 않는다 — 사용자 결정: 결정론 신뢰 불가).
        판정은 슬롯 CONTEXT(귀속 청크)만 근거로 한다(prior knowledge 불가)."""
        skipped = {"verdict": _VERDICT_SKIPPED, "rationale": "", "unsupported_claims": []}
        if self._slot_verify_source is None or not text.strip():
            return skipped
        sub_ids = {c.chunk_id for c in sub_chunks}
        prompt = "\n\n".join([
            self._slot_verify_source.prompt_body.strip(),
            "# CONTEXT\n" + self._render_context_subset(pack, sub_ids),
            "# SECTION DRAFT\n" + text,
        ])
        from app.ports.llm import GrammarSpec
        grammar = (GrammarSpec(kind="json_schema", value=self._slot_verify_source.schema)
                   if self._slot_verify_source.schema else None)
        try:
            with _TRACER.start_as_current_span("llm.slot_judge") as js:
                oi.set_kind(js, oi.KIND_EVALUATOR)
                res = await llm.generate(
                    prompt,
                    model_options=self._slot_verify_source.model_options or None,
                    grammar=grammar,
                )
                oi.set_llm(js, model_name=res.model_id, prompt=prompt,
                           completion=res.text)
        except LLMUnavailableError:
            return skipped
        import json
        try:
            obj = json.loads(res.text)
        except Exception:  # noqa: BLE001 — 파싱 실패는 판정 불가(skipped, 결정론 대체 안 함).
            return skipped
        verdict = str(obj.get("verdict", "")).lower()
        if verdict not in (_VERDICT_SUPPORTED, _VERDICT_PARTIAL, _VERDICT_UNSUPPORTED):
            # 보수적 — 알 수 없는 판정은 partial 로(supported 로 봐주지 않는다).
            verdict = _VERDICT_PARTIAL
        return {
            "verdict": verdict,
            "rationale": str(obj.get("rationale", "")).strip(),
            "unsupported_claims": [str(s) for s in obj.get("unsupported_claims", []) or []],
        }

    @classmethod
    def _render_judge_alert(cls, judge: dict[str, Any], label: str) -> str:
        """judge 결과 → 슬롯 본문 *뒤* 에 붙일 OpenWebUI alert. supported(환각 없음)면 빈 문자열
        (pass — 무표시). 그 외엔 verdict 에 맞는 alert 종류를 고르고, **본문은 모델 rationale**
        (정적 템플릿 아님 — 슬롯 단위 검증 결과 설명). 근거 미입증 문장(unsupported_claims)이
        있으면 `<details>` 로 접어 덧붙인다(OpenWebUI 렌더 지원)."""
        verdict = judge.get("verdict", _VERDICT_SUPPORTED)
        if verdict == _VERDICT_SUPPORTED:
            return ""  # 환각 없음 → pass.
        alert = _VERDICT_ALERT.get(verdict, "CAUTION")
        label_ko = {
            _VERDICT_PARTIAL: "부분 입증",
            _VERDICT_UNSUPPORTED: "근거 미입증",
            _VERDICT_SKIPPED: "검증 불가",
        }.get(verdict, "검증 주의")
        rationale = judge.get("rationale", "").strip()
        if not rationale:
            # 모델이 이유를 안 냈을 때만 최소 안내(skipped 등). 정적 단정 회피.
            rationale = ("이 구획(`%s`)의 근거 검증 설명이 제공되지 않았습니다 — 자동 검증을 "
                         "수행하지 못했습니다." % label) if verdict == _VERDICT_SKIPPED else (
                         "이 구획(`%s`)의 일부 진술이 제공 근거로 충분히 입증되지 않았습니다." % label)
        # alert 본문 = 모델이 설명한 검증 이유. blockquote 안에서 줄바꿈은 `> ` prefix.
        body_lines = "\n".join(f"> {ln}" if ln else ">"
                               for ln in rationale.splitlines())
        out = (f"> [!{alert}]\n> **근거 검증: {label_ko}** — `{label}`\n>\n{body_lines}\n")
        claims = judge.get("unsupported_claims") or []
        if claims:
            items = "\n".join(f"> - {c}" for c in claims)
            out += (">\n> <details><summary>근거 미입증 문장</summary>\n>\n"
                    + items + "\n> </details>\n")
        return out

    # ------------------------------------------------------------------
    # 재현 원본 — 슬롯별 CONTEXT + 전역 cite 맵 스냅샷(설계 §3.2). composer 는 단일 pack 을
    # write_context_snapshot 하나, pipelined 는 슬롯별 sub-pack 다수 + 전역 cite 배정이라
    # 슬롯 배열 + cite 맵을 한 파일로 남긴다(SlotCitationAllocator 상태 소멸 방지).
    # ------------------------------------------------------------------
    async def _write_pipelined_snapshot(
        self, interaction_id: str, snapshot_slots: list[dict[str, Any]],
        cites: SlotCitationAllocator,
    ) -> None:
        if not snapshot_slots:
            return
        # 전역 cite 순서(chunk_id 부여 순) — 슬롯 간 공유 chunk 재현 + References 단일화 복원.
        cite_map = {
            (getattr(c, "parent_chunk_id", None) or c.chunk_id): c.citation_id
            for c in cites.all_candidates if getattr(c, "kind", "chunk") == "chunk"
        }
        try:
            await self._sink.write_context_snapshot(
                interaction_id,
                {"schema": "context_snapshot/pipelined_v1",
                 "interaction_id": interaction_id,
                 "slots": snapshot_slots,
                 "global_cite_order": list(cites.all_chunk_ids),
                 "global_cite_map": cite_map},
            )
        except Exception:  # noqa: BLE001 — 아카이브 실패가 응답을 막지 않는다.
            _LOG.warning("context_snapshot_persist_failed",
                         interaction_id=interaction_id, variant=self.spec.variant_id)

    # ------------------------------------------------------------------
    # N4 파이프라인 — 슬롯 *생성 순서*(depends_on 위상정렬)로 future 소비 + 즉시 스트리밍.
    # ------------------------------------------------------------------
    async def _generate_pipelined(
        self, request: AgentRequest, started: float,
        tool_calls: list[ToolCallRecord], tool_result_refs: list[str], *,
        llm: LLMPort, llm_id: str, ctx: ToolExecutionContext, record,
        spec: AnswerSpec, triage, handle: SlotSearchHandle,
        triage_pin: dict[str, Any], formulation_method: str, queries,
        truncated: bool, sess: dict[str, Any], post, root,
    ) -> AgentResponse:
        # 생성 순서 = depends_on 위상정렬(composer 와 동일). 의존 슬롯이 먼저 생성돼 PRIOR 로 흐른다.
        ordered: list[SpecSlot] = (
            list(spec.slot_order()) if spec.required_slots else []
        )
        stages = self._answer_structure_stages(spec.answer_structure)

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

        slot_outputs: list[dict[str, Any]] = []
        slot_pins: list[dict[str, Any]] = []
        verify_pins: list[dict[str, Any]] = []
        fq_all: list[dict[str, Any]] = []
        streamed_parts: list[str] = []
        # 전역 citation 관리자 — 슬롯 CONTEXT 로 넘어가기 *전* 에 전역 cite-N 을 배정해 모델이
        # 처음부터 올바른 전역 번호로 생성하게 한다(사후 재번호 없음 → 스트리밍 토큰=최종 번호).
        # 중복 chunk 는 최초 등장 슬롯의 cite 재사용(References 단일화). 생성 순차라 결정적.
        cites = SlotCitationAllocator(self._context_builder)

        num_slots = len(ordered)
        # 생성 루프 전체를 try/finally 로 감싸 *어떤 종료 경로*(정상·조기 refuse·예외)에서도
        # 미소비 슬롯 검색 task 를 정리한다(orphan span/경고 방지 — 설계 §8/§7). handle.aclose
        # 는 이미 소비된 결과를 건드리지 않으므로 결정성 무관.
        try:
            return await self._run_slot_generation_loop(
                request, started, tool_calls, tool_result_refs,
                llm=llm, llm_id=llm_id, ctx=ctx, record=record,
                spec=spec, triage=triage, handle=handle,
                triage_pin=triage_pin, formulation_method=formulation_method,
                queries=queries, truncated=truncated, sess=sess, post=post,
                ordered=ordered, stages=stages, num_slots=num_slots,
                inject=inject, convo_summary=convo_summary,
                memory_refs=memory_refs, memory_ids_used=memory_ids_used,
                slot_outputs=slot_outputs, slot_pins=slot_pins,
                verify_pins=verify_pins, fq_all=fq_all,
                streamed_parts=streamed_parts, cites=cites, root=root,
            )
        finally:
            # 미소비 슬롯 검색 task 정리 + generation 부모 span 안전 종료(조기 refuse/예외
            # 경로 — 정상 경로는 루프가 이미 멱등 종료). _end_generation_span 은 루프가 핀했고,
            # 미설정(루프 진입 전 예외)이면 no-op.
            await handle.aclose()
            ender = getattr(self, "_end_generation_span", None)
            if ender is not None:
                ender()
                self._end_generation_span = None  # turn-local 핀 해제(stale 호출 방지).

    async def _run_slot_generation_loop(
        self, request: AgentRequest, started: float,
        tool_calls: list[ToolCallRecord], tool_result_refs: list[str], *,
        llm: LLMPort, llm_id: str, ctx: ToolExecutionContext, record,
        spec: AnswerSpec, triage, handle: SlotSearchHandle,
        triage_pin: dict[str, Any], formulation_method: str, queries,
        truncated: bool, sess: dict[str, Any], post,
        ordered: list[SpecSlot], stages: list[str], num_slots: int,
        inject: bool, convo_summary: str | None,
        memory_refs: tuple[MemoryRef, ...], memory_ids_used: list[str],
        slot_outputs: list[dict[str, Any]], slot_pins: list[dict[str, Any]],
        verify_pins: list[dict[str, Any]], fq_all: list[dict[str, Any]],
        streamed_parts: list[str], cites: SlotCitationAllocator, root,
    ) -> AgentResponse:
        """N4 슬롯 생성 루프 본체. `_generate_pipelined` 가 try/finally(handle.aclose)로
        감싸 호출한다 — 어떤 종료 경로든 미소비 검색 task 가 정리되도록 분리했다."""
        metrics = get_metrics()
        total_multihop = 0
        total_second_pass = 0
        total_second_necessary = 0
        total_neighbor = 0
        # 토큰 집계(D4) — 다콜(슬롯별 + 종합)이라 AgentResponse.token_usage 는 비우되,
        # Prometheus 토큰 패널이 0 으로 굶지 않도록 슬롯·종합 토큰을 합산해 record_tokens.
        # composer(_generate_single)가 단일콜에서 하는 것을 슬롯 다콜로 확장.
        total_prompt_tokens = 0
        total_completion_tokens = 0
        # TTFT(D1) — 배리어 제거의 효과를 실측하는 핵심 지표(설계 §10 #1). 첫 *본문* 토큰이
        # 흐르는 시점을 잡아 root span 에 ttft_ms 로 남긴다. None=토큰 미발생(거부/굶음).
        ttft_ms: float | None = None

        def _mark_ttft() -> None:
            nonlocal ttft_ms
            if ttft_ms is None:
                ttft_ms = (time.monotonic() - started) * 1000

        # 재현 원본(reproducibility_durable_archiving.design.v1 §3.1/§3.2) — 슬롯·종합
        # 프롬프트 *원문* 과 슬롯 CONTEXT/cite 맵을 누적해 turn 종료/실패 시 artifact 에 flush.
        # OTel span(64KB cap·만료)이 아니라 event_sink 에 남겨 만료·절단과 무관히 재현한다.
        prompt_render_calls: list[dict[str, Any]] = []
        snapshot_slots: list[dict[str, Any]] = []

        # N4 Generation 전체를 단일 부모 span(agent.generation)으로 묶는다 — 슬롯별
        # llm.slot_generation·llm.synthesize 가 그 아래로 중첩돼 Phoenix 에서 generation
        # 단계가 한 노드로 접힌다(흩어진 슬롯 span 정리). re-indent 없이 OTel context 에
        # attach/detach 해 이후 start_as_current_span 들이 이 span 을 부모로 잡게 한다.
        # 본문 토큰이 흐르기 전(루프 진입 전)에 시작해, 종합까지 포함하고 answer_text 조립
        # 전에 닫는다(N5 finalize 는 generation 밖). 예외/조기 refuse 에도 finally 로 종료.
        from opentelemetry import context as _otel_context
        from opentelemetry import trace as _otel_trace
        gen_span = _TRACER.start_span("agent.generation")
        oi.set_kind(gen_span, oi.KIND_CHAIN)
        gen_span.set_attribute("generation.num_slots", num_slots)
        _gen_token = _otel_context.attach(
            _otel_trace.set_span_in_context(gen_span))

        def _end_generation_span() -> None:
            # 멱등 — generation span 을 닫고 OTel context 를 복원한다. answer_text 조립 전(정상)
            # 1회, 조기 refuse/예외 경로의 안전판(_generate_pipelined finally)에서 1회 — 둘 중
            # 먼저 호출된 쪽만 실효(이중 detach/end 방지).
            nonlocal gen_span
            if gen_span is None:
                return
            _otel_context.detach(_gen_token)
            gen_span.end()
            gen_span = None

        # generation span 을 루프 밖에서 닫을 수 있도록 인스턴스에 핀(조기 refuse/예외 시
        # _generate_pipelined 의 finally 가 호출). 한 turn 내 단일 호출이라 안전.
        self._end_generation_span = _end_generation_span

        for idx, slot in enumerate(ordered):
            # === 슬롯 future 소비(배리어 없음 — 이 슬롯만 await; 실패는 그 슬롯만 degrade) ===
            res = await self._consume_slot_result(handle, slot.name)
            pipe = res.pipeline
            # 슬롯 검색-검증 tool_results 를 *여기서* 순차 record(결정성·순서 — §6).
            for r in pipe.tool_results:
                record(r)
            # 슬롯 검증 핀/근거 — 재현 핀(_build_qu_pin → spec_driven.verify)과 OTel 에
            # 남는 구조화 기록. rationale/rationale2(검증 근거 문장)도 함께 실어 UI thinking
            # 비활성화 후에도 관측/재현에서 검증 근거를 그대로 읽을 수 있게 한다.
            verify_pins.append({
                "slot": slot.name, "method": pipe.method,
                "num_first_pass": pipe.num_first_pass,
                "num_necessary": len(pipe.necessary),
                "num_neighbor": len(pipe.neighbor_chunks),
                "num_multihop": len(pipe.multihop_ids),
                "num_second_pass": pipe.num_second_pass,
                "num_second_necessary": len(pipe.second_pass),
                "second_method": pipe.second_method,
                "rationale": pipe.rationale,
                "rationale2": pipe.rationale2,
            })
            for fq in pipe.fq_list:
                fq_all.append(fq)
            total_multihop += len(pipe.multihop_ids)
            total_neighbor += len(pipe.neighbor_chunks)
            total_second_pass += pipe.num_second_pass
            total_second_necessary += len(pipe.second_pass)

            # === 슬롯 CONTEXT = 검증 통과 1차 ∪ 2차. cite-N 은 *생성 전* 에 전역 배정 ===
            # SlotCitationAllocator 가 이 슬롯 청크에 전역 cite-N 을 매겨 sub-pack 을 만든다 —
            # 모델은 처음부터 올바른 전역 [cite-N] 으로 생성하므로 스트리밍 토큰이 곧 최종
            # 번호다(사후 재번호 없음). 중복 chunk 는 앞선 슬롯의 cite 재사용(References 단일).
            sub_chunks = res.context_chunks[: self._slot_context_k]
            attributed = len(sub_chunks)
            fallback_ctx = not sub_chunks  # 굶음(검증이 전부 비필요) → 빈 CONTEXT.
            slot_pack = cites.build_slot_pack(
                chunks=sub_chunks,
                interaction_id=request.interaction_id,
                query_text=request.query_text,
                chat_history=request.chat_history if inject else (),
                conversation_summary=convo_summary,
                scenario_object="n_a", scenario_depth="n_a",
                entities={}, memory_refs=(), tool_result_refs=(),
            )
            sub_pack = slot_pack.pack
            allowed_cites = slot_pack.allowed_cites

            label = stages[idx] if idx < len(stages) else (slot.facet or slot.name)
            header = f"## {label}\n\n"

            await emit_step("slot_generation", "started", slot=slot.name,
                            facet=slot.facet or "-", num_chunks=len(sub_chunks),
                            index=idx)
            # NOTE: 슬롯 검색-검증(Node1) thinking 의 UI 노출을 비활성화한다. 검색-검증이
            # 생성과 병렬로 도는 파이프라인에서 이 reasoning 방출이 생성 본문 스트림과 섞여
            # 답변이 깨지는(thinking 이 본문에 새는) 현상이 있었다. 검증 근거 자체는
            # verify_pins(rationale/rationale2 포함)로 그대로 모아 재현 핀(_build_qu_pin →
            # spec_driven.verify)과 OTel span(agent.slot.<name> output_value)에 남으므로
            # 관측/재현은 영향 없다 — UI thinking 으로 흘리는 한 줄만 제거한다.
            # await emit_reasoning(
            #     f"\n**슬롯 검증 (Node1) — {slot.name}**\n"
            #     f"- [{slot.name}] 1차 {pipe.num_first_pass}개 → 필요 "
            #     f"{len(pipe.necessary)}개, 멀티홉 {len(pipe.multihop_ids)}개\n"
            # )

            # D3 — 생성 span 을 그 슬롯 *검색-검증* span(agent.slot.<name>)에 link 로 잇는다.
            # 검색 task 가 생성보다 먼저 떠서 부모-자식 중첩이 불가하므로(인과는 검색→생성),
            # OpenInference/OTel 표준대로 link 로 연결한다 → Phoenix 에서 슬롯 단위로 검색→
            # 생성 지연을 귀인. 검색 span context 가 없으면(degrade/empty) link 없이 진행.
            links = []
            search_ctx = getattr(pipe, "span_context", None)
            if search_ctx is not None:
                from opentelemetry.trace import Link
                links = [Link(search_ctx)]
            with _TRACER.start_as_current_span("llm.slot_generation",
                                               links=links) as ss:
                ss.set_attribute("slot.name", slot.name)
                ss.set_attribute("slot.facet", slot.facet or "")
                ss.set_attribute("slot.index", idx)
                ss.set_attribute("slot.num_chunks", len(sub_chunks))
                # D3 — 생성 span 만 봐도 이 슬롯이 검색에서 어떻게 걸러졌는지 보이게(검색 카운트
                # 는 agent.slot 검색 span 에만 있어 생성 span 에선 안 보이던 문제).
                ss.set_attribute("slot.num_necessary", len(pipe.necessary))
                ss.set_attribute("slot.num_second_pass", pipe.num_second_pass)
                ss.set_attribute("slot.num_neighbor", len(pipe.neighbor_chunks))
                ss.set_attribute("slot.fallback_context", fallback_ctx)
                ss.set_attribute("slot.search_method", pipe.method)
                rendered = self._render_slot_prompt(
                    request.query_text, spec, slot, sub_chunks, sub_pack,
                    prior_sections=self._prior_sections_block(slot_outputs, slot),
                    stage_index=idx, stage_total=num_slots,
                )
                slot_prompt_hash = _sha16(rendered)
                ss.set_attribute("slot.rendered_prompt_hash", slot_prompt_hash)
                # 재현 원본 누적 — 렌더 *직후*(생성 전)에 캡처해, 이 슬롯 생성이 실패해도
                # 실패 슬롯 프롬프트가 artifact 에 남는다(설계 §3.3 — 실패 turn 보존).
                prompt_render_calls.append({
                    "node": f"slot:{slot.name}", "facet": slot.facet,
                    "rendered_prompt": rendered,
                    "rendered_prompt_hash": slot_prompt_hash,
                    "model_options": self._slot_model_options(),
                    "context_chunk_ids": [c.chunk_id for c in sub_chunks],
                    "allowed_cites": sorted(allowed_cites),
                })
                snapshot_slots.append({
                    "slot": slot.name, "facet": slot.facet,
                    "chunks": [c.model_dump(mode="json") for c in sub_chunks],
                    "global_cite_chunk_ids": list(slot_pack.new_chunk_ids),
                })
                try:
                    result = await self._slot_generate_stream(
                        llm, rendered, span=ss, prefix=header,
                        model_options_override=self._slot_model_options(),
                        on_first_token=_mark_ttft,
                    )
                except LLMUnavailableError as exc:
                    ss.record_exception(exc)
                    ss.set_attribute("llm.upstream_error", str(exc)[:500])
                    _LOG.warning(
                        "llm_unavailable", node=f"slot:{slot.name}",
                        interaction_id=request.interaction_id,
                        variant=self.spec.variant_id,
                        model_id=getattr(llm, "model_id", "unknown"),
                        upstream_error=str(exc)[:500],
                        error_type=type(exc).__name__,
                    )
                    # 실패 turn 도 재현 원본 보존(설계 §3.3) — 실패 직전까지 + 실패 슬롯
                    # 프롬프트가 이미 prompt_render_calls/snapshot_slots 에 누적됨. flush 후 거부.
                    await self._record_prompt_render(
                        request.interaction_id, prompt_render_calls)
                    await self._write_pipelined_snapshot(
                        request.interaction_id, snapshot_slots, cites)
                    return await self._refuse(
                        request, started, tool_calls, RefusalReason.LLM_UNAVAILABLE,
                        error_code="llm_unavailable",
                        query_understanding=self._build_qu_pin(
                            triage_pin, spec, formulation_method, queries, truncated,
                            verify_pins, fq_all, slot_pins, sess, post,
                            total_multihop, total_second_pass, total_second_necessary,
                            cites.all_chunk_ids, total_neighbor=total_neighbor,
                        ),
                    )

            text = self._strip_leading_heading(result.text.strip())

            # === LLM-as-judge 환각 검증 + 인라인 설명 alert(design.v2 §1·§2, 사용자 결정) ===
            # 슬롯 본문이 이미 라이브 스트리밍됐으므로(streamed_before_verify) 화면을 못 되돌린다
            # → verifier 모델이 슬롯↔CONTEXT entailment 를 판정(verdict)하고 *그 이유*(rationale)를
            # 설명하면, 본문 *뒤* 에 OpenWebUI alert 로 사후 노출한다. 결정론 등급 게이트는 폐기
            # (신뢰 불가) — 환각 없으면(supported) pass(무표시), 그 외엔 모델 rationale 을 alert
            # 본문으로 보여 슬롯 단위 검증 결과를 설명한다. self-verification 금지(외부 콜).
            judge = ({"verdict": _VERDICT_SKIPPED, "rationale": "", "unsupported_claims": []}
                     if self._slot_verify == "off"
                     else await self._judge_slot(llm, text, sub_chunks, sub_pack))
            verdict = {
                "verdict": judge["verdict"],
                "rationale": judge.get("rationale", ""),
                "unsupported_claims": judge.get("unsupported_claims", []),
                "streamed_before_verify": True,
            }
            alert = ("" if self._slot_verify == "off"
                     else self._render_judge_alert(judge, label))
            ss.set_attribute("slot.verify_verdict", judge["verdict"])
            if alert:
                # 본문 직후에 alert 를 스트리밍(빈 줄로 본문과 분리). answer_text 재구성용으로도
                # 합쳐 화면=기록 일치를 보존한다.
                await emit_token("\n\n" + alert)

            # cite-N 은 이미 전역(생성 전 배정) — 사후 재매핑 불필요. 이 슬롯이 *새로* 등장
            # 시킨 chunk id(References·retrieved_chunk_ids 누적은 allocator 가 전역 보관).
            sec_global_ids = list(slot_pack.new_chunk_ids)

            section = f"{header}{text}" + (f"\n\n{alert}" if alert else "") + "\n\n"
            await emit_token("\n\n")  # 슬롯 사이 구분.
            streamed_parts.append(section)

            slot_outputs.append({"slot": slot, "header": header, "text": text})
            slot_pins.append({
                "name": slot.name, "facet": slot.facet,
                "expected_authority": slot.expected_authority,
                "context_chunk_ids": [c.chunk_id for c in sub_chunks],
                "global_cite_chunk_ids": sec_global_ids,
                "rendered_prompt_hash": slot_prompt_hash,
                "fallback_context": fallback_ctx,
                "attributed_chunks": attributed,
                "verdict": verdict,
                "verify_verdict": judge["verdict"],
                "completion_tokens": int(result.token_usage.get("completion_tokens", 0)),
            })
            total_prompt_tokens += int(result.token_usage.get("prompt_tokens", 0))
            total_completion_tokens += int(result.token_usage.get("completion_tokens", 0))
            await emit_step("slot_generation", "ok", slot=slot.name,
                            verdict=judge["verdict"])

        await emit_step("slot_search", "ok", necessary=len(cites.all_chunk_ids),
                        multihop=total_multihop, second_pass=total_second_pass)

        evidence_gap = not slot_outputs or not cites.all_chunk_ids

        # === 환각 검증 집계(design.v2 §2.2·§4) — 슬롯 verdict 분포를 전체 status 로 환산 ===
        # judge verdict 집계(판정=모델 / 집계=코드): unsupported 1개+ → FAIL, partial 만 →
        # PARTIAL, 전부 supported → PASS, 검증 슬롯 0(off/gap) → SKIPPED. skipped(verifier 미배선/
        # 실패)는 PARTIAL 로 본다(검증 못 했음을 보수적으로 — supported 로 봐주지 않는다).
        # AgentResponse.verification_status 와 smr_agent custom field 양쪽에 노출(v3.1 동형).
        verdict_counts = {v: 0 for v in
                          (_VERDICT_SUPPORTED, _VERDICT_PARTIAL,
                           _VERDICT_UNSUPPORTED, _VERDICT_SKIPPED)}
        for p in slot_pins:
            verdict_counts[p["verify_verdict"]] = \
                verdict_counts.get(p["verify_verdict"], 0) + 1
        if not slot_pins or self._slot_verify == "off":
            verification_status = VerificationStatus.SKIPPED.value
        elif verdict_counts[_VERDICT_UNSUPPORTED]:
            verification_status = VerificationStatus.FAIL.value
        elif verdict_counts[_VERDICT_PARTIAL] or verdict_counts[_VERDICT_SKIPPED]:
            verification_status = VerificationStatus.PARTIAL.value
        else:
            verification_status = VerificationStatus.PASS.value

        body_text = "".join(streamed_parts).strip()

        # === 종합(닫음 블록)만 — composer 동형(본문 재출력 금지) ===
        synth_hash: str | None = None
        closing = ""
        synth_mode = "off"
        if self._synthesize and len(slot_outputs) >= 1:
            await emit_step("synthesize", "started", num_slots=len(slot_outputs))
            with _TRACER.start_as_current_span("llm.synthesize") as sy:
                sy.set_attribute("synthesize.num_slots", len(slot_outputs))
                synth_prompt = self._render_synthesize_prompt(
                    request.query_text, spec, slot_outputs)
                synth_hash = _sha16(synth_prompt)
                sy.set_attribute("synthesize.rendered_prompt_hash", synth_hash)
                # 종합 프롬프트 원문도 재현 원본에 누적(렌더 직후).
                prompt_render_calls.append({
                    "node": "synthesize", "rendered_prompt": synth_prompt,
                    "rendered_prompt_hash": synth_hash,
                    "model_options": self._synth_model_options(),
                })
                try:
                    synth = await self._slot_generate_stream(
                        llm, synth_prompt, span=sy, prefix="\n\n",
                        model_options_override=self._synth_model_options(),
                    )
                    closing = synth.text.strip()
                    synth_mode = "model"
                    total_prompt_tokens += int(synth.token_usage.get("prompt_tokens", 0))
                    total_completion_tokens += int(
                        synth.token_usage.get("completion_tokens", 0))
                except LLMUnavailableError as exc:
                    # 종합 LLM 미가용은 거부가 아니라 닫음 블록 생략으로 degrade(본문은 이미
                    # 스트리밍됨). 핀(synth_mode)뿐 아니라 span 에도 명시해 실패를 1급으로
                    # 가시화한다(CLAUDE.md #6 — 매끈한 정상 종료로 둔갑시키지 않는다).
                    synth_mode = "skipped_unavailable"
                    sy.record_exception(exc)
                    sy.set_attribute("synthesize.skipped", True)
                    sy.set_attribute("llm.upstream_error", str(exc)[:500])
            await emit_step("synthesize", "ok", mode=synth_mode)

        # generation 단계(슬롯 루프 + 종합) 종료 — 부모 span 을 여기서 닫는다(answer_text
        # 조립·N5 finalize 는 generation 밖). 검증 집계 status 를 span 에 남겨 Phoenix 에서
        # 이 turn 의 환각 위험을 generation 노드 한 곳에서 읽는다.
        gen_span.set_attribute("generation.verification_status", verification_status)
        _end_generation_span()

        answer_text = body_text + (("\n\n" + closing) if closing else "")

        # === 답변 말미 검증 요약(design.v2 §2.2) — 환각이 검출된 슬롯이 1개+ 일 때만 1줄 노출.
        # 전부 supported/검증 off 면 군더더기 금지로 생략. 닫음 블록 *뒤*, References 앞. 카운트
        # 집계는 코드지만 슬롯별 *설명*은 위의 alert(모델 rationale)가 이미 담당한다.
        flagged = (verdict_counts[_VERDICT_UNSUPPORTED] + verdict_counts[_VERDICT_PARTIAL]
                   + verdict_counts[_VERDICT_SKIPPED])
        if self._slot_verify != "off" and flagged and slot_pins:
            parts = []
            if verdict_counts[_VERDICT_UNSUPPORTED]:
                parts.append(f"근거 미입증 {verdict_counts[_VERDICT_UNSUPPORTED]}개")
            if verdict_counts[_VERDICT_PARTIAL]:
                parts.append(f"부분 입증 {verdict_counts[_VERDICT_PARTIAL]}개")
            if verdict_counts[_VERDICT_SKIPPED]:
                parts.append(f"검증 불가 {verdict_counts[_VERDICT_SKIPPED]}개")
            summary = (
                f"\n\n> [!NOTE]\n> **근거 검증 요약** — 총 {len(slot_pins)}개 구획 중 "
                f"{verdict_counts[_VERDICT_SUPPORTED]}개 통과, " + ", ".join(parts)
                + ". 각 구획의 상세 사유는 위 검증 표시를 참고하세요."
            )
            answer_text += summary
            await emit_token(summary)

        citations = _to_citations(cites.all_candidates)
        chunk_ids = list(cites.all_chunk_ids)

        qu_pin = self._build_qu_pin(
            triage_pin, spec, formulation_method, queries, truncated,
            verify_pins, fq_all, slot_pins, sess, post,
            total_multihop, total_second_pass, total_second_necessary,
            cites.all_chunk_ids, total_neighbor=total_neighbor,
            evidence_gap=evidence_gap,
            synth_mode=synth_mode, synth_hash=synth_hash,
            verification_status=verification_status, verdict_counts=verdict_counts,
        )
        combined_hash = _sha16(
            "|".join(p["rendered_prompt_hash"] for p in slot_pins)
            + ("|" + synth_hash if synth_hash else "")
        )

        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text=answer_text,
            citations=citations,
            refusal_reason=None,
            verification_status=verification_status,
            scenario_object="n_a", scenario_depth="n_a",
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage={},
            llm_id=llm_id, model_id=getattr(llm, "model_id", "unknown"),
            regulatory_grounding="n_a",
        )
        # D4 — 슬롯·종합 토큰 합산을 Prometheus 에 기록(다콜이라 terminal token 은 비움).
        if total_prompt_tokens or total_completion_tokens:
            metrics.record_tokens(prompt_tokens=total_prompt_tokens,
                                  completion_tokens=total_completion_tokens)
        metrics.record_terminal(outcome="answer", latency_ms=response.latency_ms,
                                scenario_object="n_a", scenario_depth="n_a")

        # D1/D2 — root(agent.run) span 보강: 최종 답(output.value)·TTFT·집계 토큰·슬롯 수.
        # 루트 AGENT span 은 입력만 있고 출력/요약이 비어 trace 타일이 자명하지 않던 문제.
        oi.set_io(root, output_value=answer_text)
        root.set_attribute("agent.num_slots", len(slot_pins))
        root.set_attribute("agent.evidence_gap", evidence_gap)
        if ttft_ms is not None:
            root.set_attribute("agent.ttft_ms", round(ttft_ms, 1))
        if total_prompt_tokens or total_completion_tokens:
            root.set_attribute("agent.prompt_tokens", total_prompt_tokens)
            root.set_attribute("agent.completion_tokens", total_completion_tokens)
        if self._persona:
            root.set_attribute("agent.persona_id", self._persona.persona_id)

        # 재현 원본 영구화(설계 §3.1/§3.2) — 슬롯·종합 프롬프트 원문 + 슬롯 CONTEXT/cite 맵을
        # artifact 에 남긴다. OTel 만료·64KB cap 과 무관히 이 turn 을 바이트 단위로 재구성.
        await self._record_prompt_render(request.interaction_id, prompt_render_calls)
        await self._write_pipelined_snapshot(
            request.interaction_id, snapshot_slots, cites)

        # 최종 마무리 — composer 의 _finalize_turn(N5 세션 업데이트 + event.persist) 재사용.
        await self._finalize_turn(
            request, ctx, record, response=response, started=started,
            spec=spec, triage=triage,
            chunks=[],  # full pack 없음(slot-local) — source_id 는 fq + 슬롯 청크에서.
            chunk_ids=chunk_ids,
            fq_list=fq_all, qu_pin=qu_pin, memory_ids_used=memory_ids_used,
            sess=sess, tool_calls=tool_calls,
            prompt_profile_id="composer_pipelined_generation_v1",
            rendered_prompt_hash=combined_hash,
            prompt_composition_hash=(
                self._slot_source.policy_hash if self._slot_source else None
            ),
            context_hash=combined_hash,  # slot-local pack 다수 → 슬롯 프롬프트 해시 결합으로 대체.
        )
        return response

    # ------------------------------------------------------------------
    # 재현 핀 — composer 의 spec_driven 핀 + v2 의 verify/follow_up 섹션 통합.
    # ------------------------------------------------------------------
    def _build_qu_pin(
        self, triage_pin, spec: AnswerSpec, formulation_method: str, queries,
        truncated: bool, verify_pins: list[dict[str, Any]],
        fq_all: list[dict[str, Any]], slot_pins: list[dict[str, Any]],
        sess: dict[str, Any], post,
        total_multihop: int, total_second_pass: int, total_second_necessary: int,
        global_chunk_ids: list[str], *, total_neighbor: int = 0,
        evidence_gap: bool = False,
        synth_mode: str = "off", synth_hash: str | None = None,
        verification_status: str | None = None,
        verdict_counts: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        from app.application.agents.composer_base import _scope_summary
        return {
            "spec_driven": {
                "route": "retrieval",
                "triage": triage_pin,
                # 페르소나 재현 핀 — variant_id(=composer_pipelined_designer 등)가 이미 기록하나
                # 보강(EU AI Act Art.12 / persona_framework.design.v1 §5.3). 중립은 None.
                "persona": (
                    {"persona_id": self._persona.persona_id,
                     "profile_source_id": self._persona.profile_source_id}
                    if self._persona else None
                ),
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
                         "scope": _scope_summary(q)}
                        for q in queries
                    ],
                },
                "retrieval": {
                    "pipelined": True,  # 배리어 없는 슬롯별 검색-검증(§2).
                    "necessary_kept": len(global_chunk_ids),
                    # cite-N 은 SlotCitationAllocator 가 생성 *전* 에 전역 단일 공간으로 배정
                    # (중복 chunk 재사용). 슬롯 본문이 처음부터 전역 번호로 생성된다.
                    "cite_scope": "global_allocated",
                    "min_token_count": self._min_token_count,
                    # 노드 분리 가시화 — relevance(verify_slot)=utility_llm, multihop
                    # (follow_up)=secondary_llm 이 돈 pool id(엔드포인트). 빈 값이면 생략.
                    **({"relevance_llm_id": self._relevance_llm_id}
                       if self._relevance_llm_id else {}),
                    **({"multihop_llm_id": self._multihop_llm_id}
                       if self._multihop_llm_id else {}),
                },
                "verify": {
                    "node1": True,
                    "num_slots": len(spec.required_slots),
                    "total_necessary": len(global_chunk_ids),
                    "total_neighbor": total_neighbor,
                    "total_multihop": total_multihop,
                    "second_pass_total": total_second_pass,
                    "second_necessary_total": total_second_necessary,
                    "slots": verify_pins,
                },
                "follow_up": {
                    "necessity_only": True,
                    "num_queries": len(fq_all),
                    "queries": [
                        {"query_text": fq.get("query_text"),
                         "target_source_ids": fq.get("target_source_ids", []),
                         "intent": fq.get("intent")}
                        for fq in fq_all
                    ],
                },
                "generation": {
                    "mode": "slotwise_pipelined",
                    "num_slots": len(slot_pins),
                    "slots": slot_pins,
                    "synthesize": {"enabled": self._synthesize, "mode": synth_mode,
                                   "rendered_prompt_hash": synth_hash},
                    "slot_verify": self._slot_verify,
                    # 환각 검증 요약(design.v2 §5) — LLM-judge verdict 분포 + 전체 status.
                    # 슬롯별 verdict·rationale 은 slots[].verdict 에 이미 들어 있다(재현·감사).
                    "verification": {
                        "method": "llm_judge",
                        "status": verification_status,
                        "verdict_counts": verdict_counts or {},
                    },
                },
                "evidence_gap": evidence_gap,
                "session": self._session_pin(sess, post),
            }
        }


def _make_pipelined(
    spec: VariantSpec, deps: AgentDeps, persona: "Persona | None"
) -> "ComposerPipelinedRunner":
    """composer_pipelined 공통 팩토리 — composer(슬롯 생성) + v2(슬롯 검색-검증) source 를 함께
    주입한다. composer v2 source(N1 답변설계·N2 검색설계·슬롯 role)를 기본으로, 미배선이면
    base v1 source 로 graceful. 검증/외부참조는 retrieval.verify_slot/follow_up 도구(profiles.py
    가 배선)로 호출 — 미배선이면 단일노드 degrade(v2 동형).

    persona(composer_persona_framework.design.v1) — 생성 시점에 1개 바인딩(런타임 추론 없음).
    None=중립(현행 동작 불변, 회귀 0). composer 와 동일 fragment·동일 Persona 상수를 공유한다
    (단일 진실 — N1/N2/N4 가 같은 fragment 를 소비). persona profile source 는 미배선이면 None."""
    t = deps.tunables
    use_v2 = t.get("composer_prompts_v2", True)
    answer_spec_source = (
        getattr(deps, "composer_answer_spec_source", None) if use_v2 else None
    ) or deps.spec_driven_answer_spec_source
    query_source = (
        getattr(deps, "composer_query_source", None) if use_v2 else None
    ) or deps.spec_driven_query_source
    slot_source = (
        getattr(deps, "composer_slot_v2_source", None) if use_v2 else None
    ) or getattr(deps, "composer_slot_source", None)
    persona_sources = getattr(deps, "composer_persona_sources", None) or {}
    persona_profile_source = (
        persona_sources.get(persona.persona_id) if persona else None
    )
    # 노드 분리 가시화(원칙 5) — relevance(verify_slot)·multihop(follow_up) 둘 다 worker
    # (secondary_llm) 노드에서 돈다(profiles.py 배선). main(생성)과 물리 분리. 인스턴스의
    # model_id(HttpLLM=served-model-name, fake=fake-echo)를 핀에 노출. 미배선 시 빈 값.
    _worker_id = getattr(deps.secondary_llm, "model_id", "") if deps.secondary_llm else ""
    relevance_llm_id = _worker_id
    multihop_llm_id = _worker_id
    return ComposerPipelinedRunner(
        verify_concurrency=t.get("spec_driven_v2_verify_concurrency", 10),
        relevance_llm_id=relevance_llm_id,
        multihop_llm_id=multihop_llm_id,
        spec=spec,
        llm_router=deps.llm_router,
        tool_executor=deps.tool_executor,
        context_builder=deps.context_builder,
        recorder=deps.recorder,
        event_sink=deps.event_sink,
        app_profile=deps.app_profile,
        utility_llm=deps.utility_llm,
        answer_spec_source=answer_spec_source,
        query_source=query_source,
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
        slot_source=slot_source,
        synthesize_source=getattr(deps, "composer_synthesize_source", None),
        slot_verify_source=getattr(deps, "composer_slot_verify_source", None),
        slot_max_tokens=t.get("composer_slot_max_tokens", 8192),
        # composer_pipelined 는 환각 검증 alert 가 LLM-as-judge 에 의존하므로 검증을 *기본*
        # 활성한다(design.v2 §1 — 사용자 결정: 결정론 신뢰 불가, 모델로 검증·설명). slot_verify
        # ≠ "off" 면 _judge_slot 이 슬롯마다 verifier 모델을 호출한다. slot_verify_source(vLLM)
        # 미배선이면 judge 가 verdict="skipped" 를 돌려 [!NOTE](검증 불가) 로 솔직히 표시한다
        # (결정론 룰로 대체하지 않음). composer 의 기본(off)은 불변(A/B).
        slot_verify=t.get("composer_pipelined_slot_verify",
                          t.get("composer_slot_verify", "judge")),
        synthesize=t.get("composer_synthesize", True),
        slot_context_k=t.get("composer_slot_context_k", 12),
        prior_full_k=t.get("composer_prior_full_k", 2),
        persona=persona,
        persona_profile_source=persona_profile_source,
    )


@register_variant(COMPOSER_PIPELINED_VARIANT_ID)
def _build_composer_pipelined(
    spec: VariantSpec, deps: AgentDeps
) -> "ComposerPipelinedRunner":
    # 중립 baseline — persona 미바인딩(현행 동작 불변, 회귀 0).
    return _make_pipelined(spec, deps, persona=None)


# 페르소나 variant — `composer_pipelined_{persona_id}`. composer 와 동일 패턴: 공통 팩토리에
# Persona 상수만 바인딩, 선택은 AGENT_VARIANT 결정론(런타임 추론 없음). 같은 fragment 공유.
def _register_pipelined_persona_variants() -> None:
    for persona in PERSONAS.values():
        def _factory(spec: VariantSpec, deps: AgentDeps,
                     _persona: Persona = persona) -> "ComposerPipelinedRunner":
            return _make_pipelined(spec, deps, persona=_persona)

        register_variant(
            f"{COMPOSER_PIPELINED_VARIANT_ID}_{persona.persona_id}"
        )(_factory)


_register_pipelined_persona_variants()
