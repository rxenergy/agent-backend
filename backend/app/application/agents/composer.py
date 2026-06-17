from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from app.application.agents.events import (
    emit_reasoning,
    emit_step,
    emit_token,
)
from app.application.agents.registry import AgentDeps, register_variant
from app.application.agents.spec_driven_v1 import (
    _NOISE_FILTER,
    _SEARCH_TOOL,
    SpecDrivenRunner,
    _assemble_final_chunks,
    _parse_chunks,
    _scope_summary,
    _select_with_slot_floor,
    _sha16,
    _source_ids_of,
    _to_citations,
    _topic_signature,
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
from app.domain.spec_driven import AnswerSpec, SpecSlot
from app.observability import openinference as oi
from app.observability.logging import get_logger
from app.observability.metrics import get_metrics
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort, LLMResult, LLMUnavailableError
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")
_LOG = get_logger("agent.composer")

COMPOSER_VARIANT_ID = "composer"

# gap-answer(0-chunk)·범위 밖 cite 제거용(spec_driven_v1 와 동일 backstop).
_CITE_RE = re.compile(r"\s*\[cite-\d+\]")
# 슬롯 출력 안의 cite-N 마커 추출(L0 결정론 groundedness 게이트 — §4.1).
_CITE_N_RE = re.compile(r"\[cite-(\d+)\]")


class ComposerRunner(SpecDrivenRunner):
    """composer — spec_driven_v1 의 N0~N3.5(Triage·Define Spec·Query Formulation·
    Retrieval·Follow-up·세션메모리·재현핀)를 *그대로 계승*하고, **N4 Generation 만 슬롯
    단위 파이프라인**으로 대체하는 variant
    (docs/plans/spec_driven_slotwise_generation.design.v1.md).

    spec_driven_v1.py 는 *건드리지 않는다* — `SpecDrivenRunner` 를 상속해 N0~N3.5·세션·
    재현·gap/general 의 헬퍼 메서드(`_session_load`/`_post_gate`/`_session_update`/
    `_build_prior_context`/`_session_pin`/`_generate`/`_refuse`/`_run_general`)와 모듈
    함수(`_parse_chunks`/`_select_with_slot_floor`/`_assemble_final_chunks`/…)를 재사용하고,
    `run()` 만 오버라이드해 N4 구간을 슬롯 파이프라인으로 바꾼다.

    N4 슬롯 파이프라인:
      N4.0 Slot Plan       — required_slots 를 생성 순서로 정렬 + 슬롯별 CONTEXT 서브셋 배정
                             (slots_by_chunk 결정론, feed-narrow §3.3).
      N4.1 Slot Generate   — 슬롯당 1콜, 순차. 슬롯 i 는 이전 슬롯 *요지*(digest §3.2)를
                             참고해 누적(refine). facet 해당 축만 펼침(§6.1).
      N4.2 Slot Verify     — L0 결정론 cite-범위 게이트(항상) + L1 모델 entailment(opt-in)
                             (§4). self-verification 금지(외부 게이트).
      N4.3 Synthesize      — 슬롯 출력 전체를 재조직·일관화(grounding hard-forbid §5).
                             생략 가능(결정론 이어붙이기).

    검색은 N3 를 계승한다(현재 직렬). 외부 노드의 슬롯 단위 *병렬* 검색이 준비되면
    SlotSearchHandle 추상에 병렬 future 를 꽂아 검색-생성 파이프라인으로 지연을 단축한다
    (§1.1). gap-answer(0-chunk)·general 경로는 슬롯 분해 비대상 → 계승한 단일 경로."""

    def __init__(
        self,
        *args: Any,
        slot_source: Any = None,
        synthesize_source: Any = None,
        slot_verify_source: Any = None,
        slot_max_tokens: int = 3000,
        slot_verify: str = "off",  # "off"(기본, 현재 비활성) | "l0" | "l1"
        synthesize: bool = True,
        slot_context_k: int = 6,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # 슬롯/종합/검수 프롬프트 source(registry 호스팅). 미배선(None)이면 슬롯 프롬프트는
        # 계승한 generation_source(단일 N4 프롬프트)를 슬롯 범위로 재사용하고, 종합은
        # 결정론 이어붙이기로 떨어진다(graceful — 프롬프트 없이도 동작, 점진 도입).
        self._slot_source = slot_source
        self._synthesize_source = synthesize_source
        self._slot_verify_source = slot_verify_source
        self._slot_max_tokens = slot_max_tokens
        self._slot_verify = slot_verify
        self._synthesize = synthesize
        # 슬롯에 귀속 청크가 없을 때 결정론 fallback 으로 배정할 상위 K(슬롯 굶음 방지, §3.3).
        self._slot_context_k = slot_context_k

    # ------------------------------------------------------------------
    # run() 오버라이드 — N0~N3.5 는 base 와 동형(헬퍼 재사용), N4 만 슬롯 파이프라인.
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
                "composer prompt sources not wired — N0/N1/N2/N4/N4-G prompts are "
                "registry-hosted (prompts/registry.yaml spec_driven_* blocks)"
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
                                        prior_context=prior_context)
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
                request.query_text, spec, reasoning_label="검색 쿼리 생성")
            truncated = False
            if len(queries) > self._max_queries:
                truncated = True
                queries = queries[: self._max_queries]
            await emit_step("query_formulation", "ok", method=formulation_method,
                            num_queries=len(queries), truncated=truncated)

            # === N3 Retrieval(계승) — per-slot 멀티쿼리 + 병합 ============
            await emit_step("retrieval", "started", num_queries=len(queries))
            chunks_by_id: dict[str, RetrievedChunk] = {}
            slots_by_chunk: dict[str, set[str]] = {}
            per_query_counts: list[int] = []
            per_query_k = max(self._top_k, self._max_context_chunks)
            with _TRACER.start_as_current_span("agent.retrieval") as rs:
                for q in queries:
                    out = await self._tools.invoke(
                        _SEARCH_TOOL,
                        {"query_text": q.query_text, "top_k": per_query_k,
                         "target": q.target,
                         "min_token_count": self._min_token_count,
                         "filters": {**_NOISE_FILTER, **q.filters}},
                        ctx,
                    )
                    record(out)
                    found = _parse_chunks(out.output if out.status == "success" else None)
                    per_query_counts.append(len(found))
                    for c in found:
                        prev = chunks_by_id.get(c.chunk_id)
                        if prev is None or c.score > prev.score:
                            chunks_by_id[c.chunk_id] = c
                        slots_by_chunk.setdefault(c.chunk_id, set()).add(q.slot_name)
                rs.set_attribute("retrieval.num_chunks", len(chunks_by_id))
                oi.set_kind(rs, oi.KIND_RETRIEVER)
            merged = sorted(chunks_by_id.values(), key=lambda c: c.score, reverse=True)
            required_names = tuple(s.name for s in spec.required_slots if s.required)
            chunks, coverage = _select_with_slot_floor(
                merged, slots_by_chunk, required_names, len(merged)
            )
            first_pass_ids = {c.chunk_id for c in chunks}
            evidence_gap = not chunks
            await emit_step("retrieval", "ok", num_chunks=len(chunks),
                            merged=len(merged),
                            fetch_k=per_query_k, budget=self._max_context_chunks,
                            uncovered_required=len(coverage["uncovered_required"]),
                            evidence_gap=evidence_gap)

            # === N3.5 Follow-up(계승) ====================================
            await emit_step("follow_up_search", "started")
            follow_up_added = 0
            fq_summary: str | None = None
            fq_list: list[dict[str, Any]] = []
            searchable_count = 0
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
                            fq_summary = "\n".join(
                                f"- {fq['query_text']} → {fq.get('target_source_ids', [])}"
                                for fq in fq_list
                            )
                            searchable = [
                                fq for fq in fq_list if fq.get("target_source_ids")
                            ]
                            searchable_count = len(searchable)
                            sub_results = await asyncio.gather(
                                *(
                                    self._tools.invoke(
                                        _SEARCH_TOOL,
                                        {
                                            "query_text": fq["query_text"],
                                            "top_k": self._follow_up_fetch_k,
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
                                    continue
                                record(sub_res)
                                found = _parse_chunks(
                                    sub_res.output if sub_res.status == "success" else None
                                )
                                for c in found[: self._follow_up_keep_k]:
                                    if c.chunk_id not in chunks_by_id:
                                        chunks_by_id[c.chunk_id] = c
                                        follow_up_added += 1
                            if follow_up_added > 0:
                                merged = sorted(
                                    chunks_by_id.values(),
                                    key=lambda c: c.score, reverse=True,
                                )
                except Exception:  # noqa: BLE001 — ToolUnknown 등 graceful skip
                    pass
                fs.set_attribute("follow_up.num_queries", len(fq_list))
                fs.set_attribute("follow_up.searchable_queries", searchable_count)
                fs.set_attribute("follow_up.added_chunks", follow_up_added)
                if fq_summary:
                    oi.set_io(fs, output_value=fq_summary)
            await emit_step("follow_up_search", "ok", added_chunks=follow_up_added)

            # === 최종 조립(계승) — 1차 전량 + 2차 score 순(토큰 예산 캡) ====
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
            if fq_summary:
                await emit_reasoning(f"\n**참조 문서 재검색**\n{fq_summary}\n")

            # 재현 핀(계승 동형) — generation 백은 슬롯 N4 가 채운다.
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
                             "mode": "filter" if q.filters.get("collection")
                             else ("boost" if q.target.get("collection") else "none"),
                             "scope": _scope_summary(q)}
                            for q in queries
                        ],
                    },
                    "retrieval": {
                        "num_chunks": len(chunks),
                        "merged": len(merged),
                        "budget": self._max_context_chunks,
                        "fetch_k": per_query_k,
                        "first_pass_kept": len(first_pass_ids),
                        "per_query_counts": per_query_counts,
                        "min_token_count": self._min_token_count,
                        "filters": dict(_NOISE_FILTER),
                        "floored_slots": coverage["floored_slots"],
                        "covered_required_slots": coverage["covered_required"],
                        "uncovered_required_slots": coverage["uncovered_required"],
                    },
                    "follow_up": {
                        "num_queries": len(fq_list),
                        "added_chunks": follow_up_added,
                        "queries": [
                            {"query_text": fq.get("query_text"),
                             "target_source_ids": fq.get("target_source_ids", []),
                             "intent": fq.get("intent")}
                            for fq in fq_list
                        ],
                    },
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

            # === N4 — 슬롯 파이프라인(또는 gap-answer 시 계승 단일 경로) ====
            return await self._generate_slotwise(
                request, started, tool_calls, tool_result_refs,
                llm=llm, llm_id=llm_id, ctx=ctx, record=record,
                spec=spec, triage=triage, chunks=chunks, fq_list=fq_list,
                evidence_gap=evidence_gap, qu_pin=qu_pin,
                inject=inject, convo_summary=convo_summary,
                memory_refs=memory_refs, memory_ids_used=memory_ids_used,
                sess=sess, slots_by_chunk=slots_by_chunk,
            )

    # ==================================================================
    # N4 슬롯 파이프라인.
    # ==================================================================
    async def _generate_slotwise(
        self, request: AgentRequest, started: float,
        tool_calls: list[ToolCallRecord], tool_result_refs: list[str], *,
        llm: LLMPort, llm_id: str, ctx: ToolExecutionContext, record,
        spec: AnswerSpec, triage, chunks: list[RetrievedChunk],
        fq_list: list[dict[str, Any]], evidence_gap: bool,
        qu_pin: dict[str, Any], inject: bool, convo_summary: str | None,
        memory_refs: tuple[MemoryRef, ...], memory_ids_used: list[str],
        sess: dict[str, Any], slots_by_chunk: dict[str, set[str]],
    ) -> AgentResponse:
        metrics = get_metrics()
        plannable = list(spec.required_slots) if not evidence_gap else []

        # gap-answer(근거 0건)·슬롯 없음 → 계승한 단일 경로(슬롯 분해할 근거 없음, §7).
        if evidence_gap or not plannable:
            return await self._generate_single(
                request, started, tool_calls, tool_result_refs,
                llm=llm, llm_id=llm_id, ctx=ctx, record=record,
                spec=spec, triage=triage, chunks=chunks, fq_list=fq_list,
                evidence_gap=evidence_gap, qu_pin=qu_pin,
                inject=inject, convo_summary=convo_summary,
                memory_refs=memory_refs, memory_ids_used=memory_ids_used,
                sess=sess,
            )

        # === context_build — 전체 CONTEXT 1회(슬롯 서브셋은 파생, context_hash 동일) ==
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
        await self._sink.write_context_snapshot(
            request.interaction_id, self._context_builder.to_snapshot(pack),
        )

        # cite-N → chunk 매핑(슬롯 CONTEXT 서브셋의 허용 cite 계산 — L0 게이트 §4.1).
        cite_to_chunk: dict[str, str] = {
            cand.citation_id: (cand.parent_chunk_id or cand.chunk_id)
            for cand in pack.citation_candidates
        }
        chunk_to_cites: dict[str, list[str]] = {}
        for cite_id, chunk_id in cite_to_chunk.items():
            chunk_to_cites.setdefault(chunk_id, []).append(cite_id)

        # === N4.0 Slot Plan(결정론, feed-narrow) ======================
        plan = self._plan_slots(plannable, chunks, slots_by_chunk, spec)
        await emit_step("slot_plan", "ok", num_slots=len(plan),
                        fallback_slots=[p["name"] for p in plan if p["fallback"]])

        # === N4.1/N4.2 — 슬롯 순차 *토큰 스트리밍* 생성 + 사후 검수(verdict 기록) ======
        # 모드 A(라이브 스트리밍, 사용자 결정): 헤더 → 본문을 토큰 단위로 즉시 흘린다
        # (_slot_generate_stream). 슬롯 순서가 곧 출력 순서다(순차 실행이 순서를 자동 보장
        # — 병렬 아님). 검수(_verify_slot)는 *스트리밍 이후* verdict 기록 목적으로만 돈다:
        # 토큰이 이미 화면에 떴으므로 cite-strip/regen 같은 사후 교정은 화면을 되돌릴 수
        # 없다 — 화면에 흐른 원문이 answer_text 의 기록값이고, verdict 는 검수가 무엇을
        # 지적했는지를 핀에 남긴다(streamed_before_verify 로 분기 명시 — 재현 가능성 보존).
        slot_outputs: list[dict[str, Any]] = []
        digest_lines: list[str] = []
        slot_pins: list[dict[str, Any]] = []
        streamed_parts: list[str] = []  # 이미 화면에 흘린 본문(최종 answer_text 재구성용).
        for idx, p in enumerate(plan):
            slot: SpecSlot = p["slot"]
            sub_chunks: list[RetrievedChunk] = p["chunks"]
            allowed_cites = {
                cid for c in sub_chunks for cid in chunk_to_cites.get(c.chunk_id, ())
            }
            await emit_step("slot_generation", "started", slot=slot.name,
                            facet=slot.facet or "-", num_chunks=len(sub_chunks),
                            index=idx)
            with _TRACER.start_as_current_span("llm.slot_generation") as ss:
                ss.set_attribute("slot.name", slot.name)
                ss.set_attribute("slot.facet", slot.facet or "")
                ss.set_attribute("slot.index", idx)
                ss.set_attribute("slot.num_chunks", len(sub_chunks))
                rendered = self._render_slot_prompt(
                    request.query_text, spec, slot, sub_chunks, pack,
                    prior_digest="\n".join(digest_lines),
                )
                slot_prompt_hash = _sha16(rendered)
                ss.set_attribute("slot.rendered_prompt_hash", slot_prompt_hash)
                try:
                    # 헤더를 본문 *앞*에 prefix 로 한 번 흘리고(answer_structure 기반),
                    # 이후 본문 토큰을 라이브로 흘린다. 슬롯 사이 빈 줄은 헤더에 포함.
                    result = await self._slot_generate_stream(
                        llm, rendered, span=ss, prefix=p["header"],
                        model_options_override=self._slot_model_options(),
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
                    return await self._refuse(
                        request, started, tool_calls, RefusalReason.LLM_UNAVAILABLE,
                        error_code="llm_unavailable", query_understanding=qu_pin,
                    )

            # 화면에 흐른 원문이 기록값. 검수는 이후 verdict 기록용으로만 돈다(사후 교정은
            # 화면을 되돌릴 수 없으므로 text 는 스트리밍된 원문 유지).
            text = result.text.strip()
            _, verdict = await self._verify_slot(
                llm, slot, text, allowed_cites, sub_chunks, pack,
                request, spec, prior_digest="\n".join(digest_lines),
            )
            verdict["streamed_before_verify"] = True

            # 헤더 + 본문은 이미 토큰 단위로 흘렀다 → answer_text 재구성용으로 합치기만.
            section = f"{p['header']}{text}\n\n"
            await emit_token("\n\n")  # 슬롯 사이 구분(다음 헤더 prefix 와 합쳐 빈 줄).
            streamed_parts.append(section)

            slot_outputs.append({"slot": slot, "header": p["header"], "text": text})
            used_cites = sorted(set(_CITE_N_RE.findall(text)), key=int)
            digest_lines.append(
                f"- [{slot.name}] {self._first_sentence(text)} "
                f"(cites: {', '.join('cite-' + n for n in used_cites) or '-'})"
            )
            slot_pins.append({
                "name": slot.name, "facet": slot.facet,
                "expected_authority": slot.expected_authority,
                "context_chunk_ids": [c.chunk_id for c in sub_chunks],
                "allowed_cites": sorted(allowed_cites),
                "rendered_prompt_hash": slot_prompt_hash,
                "fallback_context": p["fallback"],
                "verdict": verdict,
                "completion_tokens": int(result.token_usage.get("completion_tokens", 0)),
            })
            await emit_step("slot_generation", "ok", slot=slot.name,
                            l0=verdict["l0"], l1=verdict.get("l1"),
                            regen=verdict.get("regen", 0))

        # 슬롯 본문은 이미 조기 스트리밍됐다 → 최종 answer_text 의 본문부는 그 합.
        body_text = "".join(streamed_parts).strip()

        # === N4.3 종합 — "정리 + 다음 액션" *닫음 블록*만(본문 재출력 금지) ==========
        # 슬롯 본문이 이미 화면에 있으므로 종합은 cross-slot 정리 + 다음 단계 제안만 만들어
        # 본문 *뒤에* 이어 스트리밍한다(사용자 결정 — 요약+다음액션). 슬롯 1개거나 종합
        # 비활성이면 닫음 블록 생략(짧은 답에 군더더기 금지).
        synth_hash: str | None = None
        closing = ""
        if self._synthesize and len(slot_outputs) >= 1:
            await emit_step("synthesize", "started", num_slots=len(slot_outputs))
            with _TRACER.start_as_current_span("llm.synthesize") as sy:
                sy.set_attribute("synthesize.num_slots", len(slot_outputs))
                synth_prompt = self._render_synthesize_prompt(
                    request.query_text, spec, slot_outputs)
                synth_hash = _sha16(synth_prompt)
                sy.set_attribute("synthesize.rendered_prompt_hash", synth_hash)
                try:
                    # 닫음 블록도 *토큰 단위 스트리밍*(사용자 요구). 본문과 사이에 빈 줄
                    # 구분(prefix)을 첫 토큰 앞에 한 번 emit한다. 슬롯 본문과 달리 검수
                    # 대상이 아니므로(검수 off·종합은 재조직) 그대로 흘린다.
                    synth = await self._slot_generate_stream(
                        llm, synth_prompt, span=sy, prefix="\n\n",
                        model_options_override=self._synth_model_options(),
                    )
                    closing = synth.text.strip()
                    synth_mode = "model"
                except LLMUnavailableError:
                    synth_mode = "skipped_unavailable"
            await emit_step("synthesize", "ok", mode=synth_mode)
        else:
            synth_mode = "off"

        # 최종 answer_text = 스트리밍된 본문 + 닫음 블록(화면에 흐른 것과 동일하게 재구성).
        answer_text = body_text + (("\n\n" + closing) if closing else "")

        citations = _to_citations(pack.citation_candidates)
        chunk_ids = [c.chunk_id for c in chunks]

        qu_pin = dict(qu_pin)
        qu_pin.setdefault("spec_driven", {})
        qu_pin["spec_driven"]["generation"] = {
            "mode": "slotwise",
            "num_slots": len(slot_pins),
            "slots": slot_pins,
            "synthesize": {"enabled": self._synthesize, "mode": synth_mode,
                           "rendered_prompt_hash": synth_hash},
            "slot_verify": self._slot_verify,
        }
        combined_hash = _sha16(
            "|".join(p["rendered_prompt_hash"] for p in slot_pins)
            + ("|" + synth_hash if synth_hash else "")
        )

        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text=answer_text,
            citations=citations,
            refusal_reason=None,
            verification_status=VerificationStatus.SKIPPED.value,
            scenario_object="n_a", scenario_depth="n_a",
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage={},  # 다콜이라 terminal token 미집계 — 슬롯 핀에 슬롯별 토큰.
            llm_id=llm_id, model_id=getattr(llm, "model_id", "unknown"),
            regulatory_grounding="n_a",
        )
        metrics.record_terminal(outcome="answer", latency_ms=response.latency_ms,
                                scenario_object="n_a", scenario_depth="n_a")

        await self._finalize_turn(
            request, ctx, record, response=response, started=started,
            spec=spec, triage=triage, chunks=chunks, chunk_ids=chunk_ids,
            fq_list=fq_list, qu_pin=qu_pin, memory_ids_used=memory_ids_used,
            sess=sess, tool_calls=tool_calls,
            prompt_profile_id="composer_generation_slotwise_v1",
            rendered_prompt_hash=combined_hash,
            prompt_composition_hash=(
                self._slot_source.policy_hash if self._slot_source else None
            ),
            context_hash=pack.context_hash,
        )
        return response

    # ------------------------------------------------------------------
    # gap-answer/슬롯없음 — 계승한 단일 N4 경로를 *복제*(spec_driven_v1 불변). 단일 호출
    # generation + N5 + persist. base 의 _render_generation_prompt/_generate/_finalize_turn
    # (헬퍼)을 재사용한다.
    # ------------------------------------------------------------------
    async def _generate_single(
        self, request: AgentRequest, started: float,
        tool_calls: list[ToolCallRecord], tool_result_refs: list[str], *,
        llm: LLMPort, llm_id: str, ctx: ToolExecutionContext, record,
        spec: AnswerSpec, triage, chunks: list[RetrievedChunk],
        fq_list: list[dict[str, Any]], evidence_gap: bool,
        qu_pin: dict[str, Any], inject: bool, convo_summary: str | None,
        memory_refs: tuple[MemoryRef, ...], memory_ids_used: list[str],
        sess: dict[str, Any],
    ) -> AgentResponse:
        metrics = get_metrics()
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
            request.query_text, pack, spec, evidence_gap=evidence_gap)
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
            return llm_result
        await emit_step("generation", "ok",
                        completion_tokens=llm_result.token_usage.get("completion_tokens", 0))
        metrics.record_tokens(
            prompt_tokens=int(llm_result.token_usage.get("prompt_tokens", 0)),
            completion_tokens=int(llm_result.token_usage.get("completion_tokens", 0)),
        )

        citations = _to_citations(pack.citation_candidates)
        chunk_ids = [c.chunk_id for c in chunks]
        terminal_outcome = "answer_with_gaps" if evidence_gap else "answer"
        answer_text = llm_result.text
        if evidence_gap:
            answer_text = _CITE_RE.sub("", answer_text).strip()

        response = AgentResponse(
            interaction_id=request.interaction_id,
            answer_text=answer_text,
            citations=citations,
            refusal_reason=None,
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
        await self._finalize_turn(
            request, ctx, record, response=response, started=started,
            spec=spec, triage=triage, chunks=chunks, chunk_ids=chunk_ids,
            fq_list=fq_list, qu_pin=qu_pin, memory_ids_used=memory_ids_used,
            sess=sess, tool_calls=tool_calls,
            prompt_profile_id="spec_driven_generation_v1",
            rendered_prompt_hash=rendered_prompt_hash,
            prompt_composition_hash=self._generation_source.policy_hash,
            context_hash=pack.context_hash,
        )
        return response

    # ------------------------------------------------------------------
    # N5 Session Update + event.persist — 단일/슬롯 경로 공통 마무리(중복 금지).
    # ------------------------------------------------------------------
    async def _finalize_turn(
        self, request: AgentRequest, ctx: ToolExecutionContext, record, *,
        response: AgentResponse, started: float, spec: AnswerSpec, triage,
        chunks: list[RetrievedChunk], chunk_ids: list[str],
        fq_list: list[dict[str, Any]], qu_pin: dict[str, Any],
        memory_ids_used: list[str], sess: dict[str, Any],
        tool_calls: list[ToolCallRecord], prompt_profile_id: str,
        rendered_prompt_hash: str, prompt_composition_hash: str | None,
        context_hash: str,
    ) -> None:
        await self._session_update(
            request, ctx, record,
            user_turn=request.query_text, assistant_turn=response.answer_text,
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
                prompt_profile_id=prompt_profile_id,
                prompt_version="v1",
                rendered_prompt_hash=rendered_prompt_hash,
                prompt_composition_hash=prompt_composition_hash,
                prompt_source="local",
                context_hash=context_hash,
                started_at=started,
                tool_calls=tuple(tool_calls),
                regulatory_grounding="n_a",
                query_understanding=qu_pin,
                memory_ids_used=tuple(memory_ids_used),
                memory_types_used=tuple("session" for _ in memory_ids_used),
            )
            await self._recorder.persist(event)
            s.set_attribute("interaction_id", request.interaction_id)

    # ------------------------------------------------------------------
    # N4.0 — 슬롯 순서 + 슬롯별 CONTEXT 서브셋(결정론). 귀속(slots_by_chunk)으로 고르고
    # 귀속 0이면 score 상위 K fallback(슬롯 굶음 방지, §3.3). required 먼저, supporting 뒤
    # — N1 산출 순서 보존(재정렬 룰 없음, 표현=모델).
    # ------------------------------------------------------------------
    def _plan_slots(
        self, slots: list[SpecSlot], chunks: list[RetrievedChunk],
        slots_by_chunk: dict[str, set[str]], spec: AnswerSpec,
    ) -> list[dict[str, Any]]:
        ordered = [s for s in slots if s.required] + [s for s in slots if not s.required]
        # answer_structure 단계명을 순서대로 슬롯 헤더에 매핑(사용자 결정 — answer_structure
        # 기반 헤더). "지배조문→요건→예외" 같은 화살표/구분자 분해. 단계 수가 슬롯 수와
        # 다르면 매핑 안 된 슬롯은 facet/슬롯명으로 fallback(결정론, silent 아님).
        stages = self._answer_structure_stages(spec.answer_structure)
        by_score = sorted(chunks, key=lambda c: c.score, reverse=True)
        plan: list[dict[str, Any]] = []
        for i, s in enumerate(ordered):
            owned = [c for c in by_score
                     if s.name in slots_by_chunk.get(c.chunk_id, set())]
            fallback = False
            if not owned:
                owned = by_score[: self._slot_context_k]
                fallback = True
            else:
                owned = owned[: self._slot_context_k]
            label = stages[i] if i < len(stages) else (s.facet or s.name)
            plan.append({"slot": s, "name": s.name, "chunks": owned,
                         "fallback": fallback, "header": f"## {label}\n\n"})
        return plan

    @staticmethod
    def _answer_structure_stages(answer_structure: str | None) -> list[str]:
        """answer_structure 한 줄을 단계명 리스트로 분해(헤더용). 화살표(→/->)·중점(·)·
        파이프·쉼표를 구분자로. 괄호 안 하위 facet 은 헤더에서 제거(간결). 빈 값=[]."""
        if not answer_structure:
            return []
        s = re.sub(r"\([^)]*\)", "", answer_structure)  # 괄호 하위 facet 제거.
        parts = re.split(r"\s*(?:→|->|·|\||,|;)\s*", s)
        return [p.strip() for p in parts if p.strip()]

    # ------------------------------------------------------------------
    # N4.1 — 슬롯 1개 생성 프롬프트. slot_source 미배선이면 계승한 generation_source(단일
    # N4 본문)를 쓰고 슬롯 trailer 만 덧댄다(graceful). 배치: [본문][이전 요지][# CONTEXT
    # 서브셋][이 슬롯 지시][QUERY][lang] — 핵심 지시·질의를 CONTEXT 뒤(recency §6.1).
    # ------------------------------------------------------------------
    def _render_slot_prompt(
        self, query_text: str, spec: AnswerSpec, slot: SpecSlot,
        sub_chunks: list[RetrievedChunk], pack, *, prior_digest: str,
    ) -> str:
        body = (self._slot_source.prompt_body if self._slot_source
                else self._generation_source.prompt_body).strip()
        parts = [body]
        if self._citation_contract:
            parts.append("# CITATION CONTRACT\n" + self._citation_contract.strip())
        if prior_digest.strip():
            # 프롬프트(composer_slot_v1.md)가 기대하는 섹션명 — 이전 슬롯 요지(연속성·중복
            # 회피용 맥락이지 근거 아님). 슬롯명·첫 문장·사용 cite-ID 만(전문 아님 §3.2).
            parts.append(
                "# PRIOR SECTIONS (앞서 작성돼 사용자에게 이미 보인 구획의 요지 — 이어쓰되\n"
                "중복하지 말고, 여기 적힌 내용을 근거로 재사용하지 말고 이 섹션 CONTEXT 로만 근거하라)\n"
                + prior_digest.strip()
            )
        sub_ids = {c.chunk_id for c in sub_chunks}
        parts.append("# CONTEXT\n" + self._render_context_subset(pack, sub_ids))
        tag = f" [{slot.facet}]" if slot.facet else ""
        auth = (f"\nexpected_authority: {slot.expected_authority}"
                if slot.expected_authority else "")
        parts.append(
            f"# THIS SECTION{tag}\n"
            f"slot: {slot.name}\n"
            f"answer_structure: {spec.answer_structure or '-'}\n"
            f"governing_normative_class: {spec.governing_normative_class or '-'}{auth}\n"
            f"무엇을 확립할 것인가: {slot.description or slot.name}\n"
            "위 CONTEXT 근거만으로 이 구획을 전문가 깊이로 작성하라. 다른 구획이 다룰 내용은\n"
            "겹쳐 쓰지 마라. CONTEXT 가 이 구획을 뒷받침하지 못하면 그 한계를 명시하라."
        )
        parts.append("# QUERY\n" + query_text)
        parts.append(
            "# RESPONSE LANGUAGE\n"
            "Write this section in the same language as the QUERY above "
            "(Korean query → Korean answer). Citation markers and source ids stay verbatim."
        )
        return "\n\n".join(parts)

    def _render_context_subset(self, pack, chunk_ids: set[str]) -> str:
        """전체 pack 에서 지정 chunk_id 들만 추린 # CONTEXT 본문(슬롯 서브셋, feed-narrow
        §3.3). pack.py render_for_prompt 와 동형(full 본문·표 마커 제거 규약 동일)."""
        from app.application.context.pack import (
            _render_table_entry,
            _strip_table_markers,
        )

        by_id = {c.chunk_id: c for c in pack.chunks}
        lines: list[str] = []
        for cand in pack.citation_candidates:
            if cand.kind != "chunk":
                continue
            cid = cand.parent_chunk_id or cand.chunk_id
            if cid not in chunk_ids:
                continue
            chunk = by_id.get(cid)
            head = cand.formatted or (
                f"[{cand.citation_id}] {cand.document_id}#{cand.chunk_id} (p={cand.page})"
            )
            if chunk is None:
                lines.append(f"{head}\n(chunk unavailable)")
                continue
            if pack.capture_mode == "full" and chunk.text:
                bdy = chunk.text
            elif pack.capture_mode in ("snippets", "full") and chunk.snippet:
                bdy = chunk.snippet
            else:
                bdy = "(metadata-only capture)"
            lines.append(f"{head}\n{_strip_table_markers(bdy)}")
        tbl: list[str] = []
        for cand in pack.citation_candidates:
            if cand.kind != "table" or not cand.tables:
                continue
            if (cand.parent_chunk_id or cand.chunk_id) not in chunk_ids:
                continue
            rendered = _render_table_entry(cand.tables[0])
            if rendered is None:
                continue
            src = f"{cand.document_id or '?'}"
            if cand.page is not None:
                src += f", p. {cand.page}"
            tbl.append(f"[{cand.citation_id}] (표 — {src})\n{rendered}")
        out = "\n\n".join(lines) if lines else "(no retrieved context for this section)"
        if tbl:
            out += "\n\n# TABLES\n" + "\n\n".join(tbl)
        return out

    # ------------------------------------------------------------------
    # N4.3 — 종합(닫음) 프롬프트. 슬롯 본문은 *이미 스트리밍됨* → 종합은 본문 재출력 금지,
    # "핵심 정리 + 다음 단계 제안"만(사용자 결정). synthesize_source 미배선이면 인라인 지시.
    # 입력의 `# SECTIONS ALREADY SHOWN` 은 이미 화면에 있는 본문(정리 대상이지 재출력 대상
    # 아님)임을 프롬프트가 명시한다.
    # ------------------------------------------------------------------
    def _render_synthesize_prompt(
        self, query_text: str, spec: AnswerSpec,
        slot_outputs: list[dict[str, Any]],
    ) -> str:
        if self._synthesize_source:
            body = self._synthesize_source.prompt_body.strip()
        else:
            body = (
                "The section-by-section body of this answer has ALREADY been shown to the "
                "reader. Do NOT repeat, rewrite, or re-cite it. Produce only a short closing "
                "block: '## 핵심 정리' (3–6 one-line bullets synthesizing across the sections "
                "— the through-line and any tension/gap they reveal together) then '## 다음 "
                "단계 제안' (2–4 actionable next steps grounded in what the answer established "
                "or left open). Add no new regulatory fact or [cite-N]. Same language as QUERY."
            )
        parts = [body, "# ANSWER STRUCTURE\n" + (spec.answer_structure or "-")]
        sec_lines = [f"## SECTION [{o['slot'].name}]\n{o['text']}" for o in slot_outputs]
        parts.append("# SECTIONS ALREADY SHOWN\n" + "\n\n".join(sec_lines))
        parts.append("# QUERY\n" + query_text)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # N4.2 — 슬롯 검수. L0 결정론 cite-범위(항상) + L1 모델 entailment(opt-in). 위반 시
    # 1회 재생성, 재생성 후에도 위반이면 범위 밖 cite 제거 + 한계 inline 표기(강등).
    # self-verification 금지: L1 은 별도 판정 호출(생성과 분리, §4).
    # ------------------------------------------------------------------
    async def _verify_slot(
        self, llm: LLMPort, slot: SpecSlot, text: str, allowed_cites: set[str],
        sub_chunks: list[RetrievedChunk], pack, request: AgentRequest,
        spec: AnswerSpec, *, prior_digest: str,
    ) -> tuple[str, dict[str, Any]]:
        verdict: dict[str, Any] = {"l0": "pass", "l1": None, "regen": 0}
        if self._slot_verify == "off":
            return text, verdict

        def _out_of_range(t: str) -> set[str]:
            used = {f"cite-{n}" for n in _CITE_N_RE.findall(t)}
            return used - allowed_cites

        oor = _out_of_range(text)
        if oor:
            verdict["l0"] = "violation"
            verdict["l0_out_of_range"] = sorted(oor)
            for cid in oor:
                text = text.replace(f"[{cid}]", "")
            text = re.sub(r"[ \t]{2,}", " ", text).strip()

        if self._slot_verify == "l1" and self._slot_verify_source is not None:
            verdict["l1"] = await self._l1_entailment(llm, text, sub_chunks, pack)
            if verdict["l1"] == "unsupported":
                regen = await self._regenerate_slot(
                    llm, slot, sub_chunks, pack, request, spec, prior_digest)
                if regen is not None:
                    verdict["regen"] = 1
                    text = regen
                    if _out_of_range(text):
                        for cid in _out_of_range(text):
                            text = text.replace(f"[{cid}]", "")
                        text = re.sub(r"[ \t]{2,}", " ", text).strip()
                    verdict["l1_after_regen"] = await self._l1_entailment(
                        llm, text, sub_chunks, pack)
                    if verdict["l1_after_regen"] == "unsupported":
                        text += "\n\n*(근거 부족: 이 구획은 CONTEXT 로 충분히 입증되지 않음)*"
        return text, verdict

    async def _l1_entailment(
        self, llm: LLMPort, text: str,
        sub_chunks: list[RetrievedChunk], pack,
    ) -> str:
        """슬롯 출력 ↔ 슬롯 CONTEXT entailment 판정(structured, 판정만 — self-verification
        금지). enum: supported/partial/unsupported. 미배선/실패=skipped/partial."""
        if self._slot_verify_source is None:
            return "skipped"
        sub_ids = {c.chunk_id for c in sub_chunks}
        prompt = "\n\n".join([
            self._slot_verify_source.prompt_body.strip(),
            "# CONTEXT\n" + self._render_context_subset(pack, sub_ids),
            "# SECTION DRAFT\n" + text,
        ])
        grammar = (GrammarSpec(kind="json_schema", value=self._slot_verify_source.schema)
                   if self._slot_verify_source.schema else None)
        try:
            res = await llm.generate(
                prompt, model_options=self._slot_verify_source.model_options or None,
                grammar=grammar,
            )
        except LLMUnavailableError:
            return "skipped"
        import json
        try:
            v = str(json.loads(res.text).get("verdict", "partial")).lower()
            return v if v in ("supported", "partial", "unsupported") else "partial"
        except Exception:  # noqa: BLE001
            return "partial"

    async def _regenerate_slot(
        self, llm: LLMPort, slot: SpecSlot, sub_chunks: list[RetrievedChunk],
        pack, request: AgentRequest, spec: AnswerSpec, prior_digest: str,
    ) -> str | None:
        prompt = self._render_slot_prompt(
            request.query_text, spec, slot, sub_chunks, pack,
            prior_digest=prior_digest)
        prompt += ("\n\n# CORRECTION\n이전 초안이 CONTEXT 밖 주장을 포함했다. CONTEXT 가 "
                   "직접 뒷받침하는 사실만 남기고 추론·일반론을 제거해 다시 작성하라.")
        try:
            with _TRACER.start_as_current_span("llm.slot_regenerate") as ss:
                ss.set_attribute("slot.name", slot.name)
                res = await self._slot_generate(llm, prompt, span=ss)
            return res.text
        except LLMUnavailableError:
            return None

    # ------------------------------------------------------------------
    # 슬롯/종합 LLM 호출 — 모드 B(비스트리밍 생성 → 검수 → 최종만 스트리밍, §5.3). 슬롯
    # 본문은 검수 전 노출하지 않으므로 항상 비스트리밍 generate. span 에 LLM IO 기록.
    # ------------------------------------------------------------------
    async def _slot_generate(
        self, llm: LLMPort, prompt: str, *, span,
        model_options_override: dict[str, Any] | None = None,
    ) -> LLMResult:
        opts = model_options_override or self._slot_model_options()
        res = await llm.generate(prompt, model_options=opts)
        span.set_attribute("model_id", res.model_id)
        oi.set_kind(span, oi.KIND_LLM)
        oi.set_llm(span, model_name=res.model_id, prompt=prompt, completion=res.text,
                   prompt_tokens=int(res.token_usage.get("prompt_tokens", 0)),
                   completion_tokens=int(res.token_usage.get("completion_tokens", 0)))
        return res

    async def _slot_generate_stream(
        self, llm: LLMPort, prompt: str, *, span,
        prefix: str = "", model_options_override: dict[str, Any] | None = None,
    ) -> LLMResult:
        """토큰 단위 스트리밍 생성 — 종합(닫음 블록)처럼 *검수 없이 그대로 흘리는* 출력에
        쓴다(슬롯 본문은 검수 후 노출해야 하므로 비스트리밍 _slot_generate). `prefix` 는
        본문과 닫음 블록 사이 구분(빈 줄 등)을 첫 토큰 *앞*에 한 번 emit 한다. 누적 텍스트를
        LLMResult 로 돌려줘 호출부가 answer_text 재구성·cite 가드에 쓴다."""
        opts = model_options_override or self._synth_model_options()
        text_buf: list[str] = []
        token_usage: dict[str, int] = {}
        model_id: str | None = None
        first = True
        async for delta in llm.generate_stream(prompt, model_options=opts):
            if delta.content:
                if first and prefix:
                    await emit_token(prefix)
                    first = False
                text_buf.append(delta.content)
                await emit_token(delta.content)
            if delta.token_usage:
                token_usage = dict(delta.token_usage)
            if delta.model_id:
                model_id = delta.model_id
        text = "".join(text_buf)
        span.set_attribute("model_id", model_id or getattr(llm, "model_id", "unknown"))
        oi.set_kind(span, oi.KIND_LLM)
        oi.set_llm(span, model_name=model_id or "unknown", prompt=prompt,
                   completion=text,
                   prompt_tokens=int(token_usage.get("prompt_tokens", 0)),
                   completion_tokens=int(token_usage.get("completion_tokens", 0)))
        return LLMResult(
            text=text,
            token_usage=token_usage or {"prompt_tokens": 0,
                                        "completion_tokens": len(text)},
            model_id=model_id or getattr(llm, "model_id", "unknown"),
        )

    # ------------------------------------------------------------------
    # 보조 — model_options / 이어붙이기 / 요지 추출 / cite 범위 가드.
    # ------------------------------------------------------------------
    def _slot_model_options(self) -> dict[str, Any]:
        if self._slot_source and self._slot_source.model_options:
            opts = dict(self._slot_source.model_options)
        else:
            opts = dict(self._generation_source.model_options or {})
        opts["max_tokens"] = min(int(opts.get("max_tokens", self._slot_max_tokens)),
                                 self._slot_max_tokens)
        return opts

    def _synth_model_options(self) -> dict[str, Any]:
        if self._synthesize_source and self._synthesize_source.model_options:
            return dict(self._synthesize_source.model_options)
        return dict(self._generation_source.model_options or {})

    @staticmethod
    def _first_sentence(text: str, *, limit: int = 200) -> str:
        t = _CITE_RE.sub("", re.sub(r"\s+", " ", text.strip()))
        m = re.search(r"[.!?。]\s", t)
        s = t[: m.start() + 1] if m else t
        return s[:limit].strip()

    @staticmethod
    def _strip_out_of_range_cites(text: str, allowed: set[str]) -> str:
        def _sub(m: re.Match[str]) -> str:
            return m.group(0) if f"cite-{m.group(1)}" in allowed else ""
        return re.sub(r"[ \t]{2,}", " ", _CITE_N_RE.sub(_sub, text)).strip()


@register_variant(COMPOSER_VARIANT_ID)
def _build_composer(spec: VariantSpec, deps: AgentDeps) -> "ComposerRunner":
    t = deps.tunables
    return ComposerRunner(
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
        # composer 전용 — 신규 source(미배선이면 graceful) + tunable.
        slot_source=getattr(deps, "composer_slot_source", None),
        synthesize_source=getattr(deps, "composer_synthesize_source", None),
        slot_verify_source=getattr(deps, "composer_slot_verify_source", None),
        slot_max_tokens=t.get("composer_slot_max_tokens", 3000),
        slot_verify=t.get("composer_slot_verify", "off"),
        synthesize=t.get("composer_synthesize", True),
        slot_context_k=t.get("composer_slot_context_k", 6),
    )
