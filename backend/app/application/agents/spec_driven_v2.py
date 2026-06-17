"""spec_driven_v2 — 2-노드(DGX Spark) 분산 검색 Agent.

설계: docs/plans/spec_driven_agent.design.v2.md (계획 1-agile-cook).

spec_driven_v1 의 선형 4-노드 흐름을 그대로 상속하되, LLM 업무를 **두 vLLM 노드**로
분할한다:
  - **Node1**(main = 요청 resolved LLM = utility_llm/default_llm): N1 spec/slot 생성, N2
    슬롯별 검색 쿼리, 슬롯 단위 1차 검색 결과 **검증**(retrieval.verify_slot) — 답변에 꼭
    필요한 청크 식별자 + 멀티홉 필요 청크 선별, N4 생성.
  - **Node2**(sub = SECONDARY_LLM = gemma-4-26b-sub): 외부 참조 문서 선별
    (enhanced retrieval.follow_up) — 멀티홉 청크에서 재검색 대상 참조 문서를 고른다.

핵심은 `_post_retrieval` 시임 오버라이드다: 슬롯별 파이프라인(Node1 검증 → Node2 외부참조
선별 → 2차 검색)을 배리어 없이 동시 실행해, slot 1 이 Node2 처리로 넘어갈 때 slot 2 는
이미 Node1 검증을 돈다(두 노드가 별개 vLLM 이라 실제 겹침). 결정성은 v1 idiom(gather 후
고정 슬롯 순 순차 병합)으로 보존한다. N4 컨텍스트는 v1 의 "1차 전량 보존"을 폐기하고
Node1 이 고른 **필요 청크 + 2차(멀티홉) 결과만** 쓴다(사용자 결정 #4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace

from app.application.agents.events import emit_step
from app.application.agents.registry import AgentDeps, register_variant
from app.application.agents.spec_driven_v1 import (
    _NOISE_FILTER,
    _SEARCH_TOOL,
    SpecDrivenRunner,
    _PostRetrievalOutcome,
    _assemble_final_chunks,
    _parse_chunks,
    _render_spec_block,
)
from app.domain.agents import VariantSpec
from app.domain.retrieval import RetrievedChunk
from app.domain.spec_driven import AnswerSpec, FormulatedQuery
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.tool import ToolExecutionContext

_TRACER = get_tracer("agent")

SPEC_DRIVEN_V2_VARIANT_ID = "spec_driven_v2"

_VERIFY_TOOL = "retrieval.verify_slot"
_FOLLOW_UP_TOOL = "retrieval.follow_up"


@dataclass
class _SlotPipelineResult:
    """한 슬롯의 파이프라인 산출(verify→ref_select→second_pass). 병합은 run()/seam 이
    슬롯 원순서대로 *순차* 처리한다(race 방지·결정성 — v1 idiom)."""

    slot_name: str
    method: str                                       # "llm" | "fallback"
    num_first_pass: int
    necessary: list[RetrievedChunk]                   # Node1 이 고른 필요 청크
    multihop_ids: list[str]                            # 멀티홉 필요 청크 id
    rationale: str
    fq_list: list[dict[str, Any]] = field(default_factory=list)   # Node2 외부참조 선별 결과
    second_pass: list[RetrievedChunk] = field(default_factory=list)  # Stage4 검증 *통과* 2차 청크
    tool_results: list[Any] = field(default_factory=list)         # record() 용(순차 기록)
    # Stage 4 — 2차 검색 결과 Node1 재검증(검색 후엔 항상 relevance). second_pass 는 이미
    # 통과 청크만 담는다(검증 후 trim). num_second_pass 는 검증 *입력* 수, second_method 는
    # 재검증 경로("llm"|"fallback"|"skip"), rationale2 는 재검증 근거(UI thinking 노출용).
    num_second_pass: int = 0
    second_method: str = "skip"
    rationale2: str = ""


class SpecDrivenV2Runner(SpecDrivenRunner):
    """spec_driven_v2 러너 — v1 의 4-노드 선형 흐름을 상속하고, N3.5+최종조립 시임만
    per-slot 2-노드 파이프라인으로 오버라이드한다. 프롬프트 profile_id 라벨은 `*_v2` 로
    분리(재현 핀이 v2 정책 산출임을 단독 설명 — 원칙 5)."""

    _GENERATION_PROFILE_ID = "spec_driven_generation_v2"
    _GENERAL_PROFILE_ID = "spec_driven_general_v2"

    def __init__(self, *, verify_concurrency: int = 10, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # 동시 슬롯 파이프라인 상한 — Node1 검증 도구도 자체 semaphore 를 갖지만, 러너 레벨
        # 캡이 Node2 2차 검색 fan-out 까지 함께 묶는다. 실제 Node1 보호는 SlotVerifierLlm 의
        # 청크별 전역 캡이 담당하므로 이 슬롯 캡은 최대 슬롯 수(≤10)에 맞춰 넉넉히 둔다.
        self._verify_sem = asyncio.Semaphore(max(1, verify_concurrency))

    async def _post_retrieval(
        self, *, request, ctx: ToolExecutionContext, record,
        spec: AnswerSpec, queries: list[FormulatedQuery],
        merged: list[RetrievedChunk], chunks: list[RetrievedChunk],
        chunks_by_id: dict[str, RetrievedChunk],
        slots_by_chunk: dict[str, set[str]], first_pass_ids: set[str],
        coverage: dict[str, list[str]], per_query_counts: list[int],
        per_query_k: int,
    ) -> "_PostRetrievalOutcome":
        """v2 — per-slot 파이프라인(Node1 검증 → Node2 외부참조 선별 → 2차 검색)을 배리어
        없이 동시 실행하고, 결정성을 위해 gather 후 슬롯 원순서대로 순차 병합한다. N4
        컨텍스트는 Node1 이 고른 필요 청크 ∪ 2차 결과만(1차 전량 보존 폐기)."""
        spec_block = _render_spec_block(spec)
        # 슬롯별 1차 청크(슬롯 내 score desc). chunks 는 floor 정렬된 1차 전량.
        slot_order = [s.name for s in spec.required_slots]
        # slot_query: 슬롯명 → 그 슬롯의 N2 검색 쿼리 텍스트(없으면 원질의 폴백).
        slot_query_by_name: dict[str, str] = {}
        for q in queries:
            slot_query_by_name.setdefault(q.slot_name, q.query_text)
        chunks_by_slot: dict[str, list[RetrievedChunk]] = {n: [] for n in slot_order}
        for c in chunks:  # chunks 는 score desc
            for sn in slots_by_chunk.get(c.chunk_id, ()):  # 한 청크가 여러 슬롯에 귀속 가능
                if sn in chunks_by_slot:
                    chunks_by_slot[sn].append(c)

        await emit_step("slot_verify", "started", num_slots=len(slot_order))
        # 슬롯 파이프라인은 배리어 없이 gather 로 동시 실행한다. 각 슬롯의 도구 호출(검증·
        # 외부참조 선별·2차 검색)은 _run_slot_pipeline 안에서 연 `agent.slot.<name>` span
        # 하나의 자식으로 자연히 묶인다 — gather task 마다 contextvars 가 독립이라 슬롯끼리
        # 섞이지 않는다(Phoenix 에서 agent.run > agent.slot.<name> > {도구} 로 읽힌다).
        # 전역 집계 속성은 아래에서 현재 span(=agent.run)에 단다.

        async def _pipeline_slot(slot_name: str) -> _SlotPipelineResult:
            slot_chunks = chunks_by_slot.get(slot_name, [])
            async with self._verify_sem:
                return await self._run_slot_pipeline(
                    request=request, ctx=ctx, spec_block=spec_block,
                    slot_name=slot_name,
                    slot_query=slot_query_by_name.get(slot_name, request.query_text),
                    slot_chunks=slot_chunks,
                )

        # 배리어 없는 동시 실행 — task 로 띄워 slot 들이 두 노드에서 겹쳐 돈다.
        slot_results = await asyncio.gather(
            *(asyncio.create_task(_pipeline_slot(n)) for n in slot_order),
            return_exceptions=True,
        )

        # === 결정성 병합 — 슬롯 원순서대로 순차(완료순 X). 모든 변이는 여기서만 ===
        necessary_by_id: dict[str, RetrievedChunk] = {}
        second_by_id: dict[str, RetrievedChunk] = {}
        verify_pins: list[dict[str, Any]] = []
        fq_all: list[dict[str, Any]] = []
        fq_summary_lines: list[str] = []
        # UI thinking 노출용 — 슬롯별 Node1 검증/재검증 근거(B2: rationale 까지). task
        # 내부가 아니라 여기서 버퍼만 하고, base run() 이 단계 종료 후 emit(순서·race 안전).
        verify_reason_lines: list[str] = []
        total_multihop = 0
        total_second_pass = 0
        total_second_necessary = 0
        slots_with_multihop = 0
        for slot_name, res in zip(slot_order, slot_results):
            if isinstance(res, BaseException):
                # 슬롯 파이프라인 전체 실패 → 그 슬롯은 기여 없음(graceful). 핀에 남긴다.
                verify_pins.append({"slot": slot_name, "method": "error",
                                    "num_first_pass": len(chunks_by_slot.get(slot_name, [])),
                                    "num_necessary": 0, "num_multihop": 0,
                                    "num_second_pass": 0, "num_second_necessary": 0,
                                    "second_method": "error"})
                verify_reason_lines.append(f"- [{slot_name}] 검증 실패(graceful skip)")
                continue
            for r in res.tool_results:  # record 는 race 방지 위해 여기서만(순차)
                record(r)
            for c in res.necessary:
                if c.chunk_id not in necessary_by_id:
                    necessary_by_id[c.chunk_id] = c
            for c in res.second_pass:
                if c.chunk_id not in necessary_by_id and c.chunk_id not in second_by_id:
                    second_by_id[c.chunk_id] = c
            total_multihop += len(res.multihop_ids)
            if res.multihop_ids:
                slots_with_multihop += 1
            total_second_pass += res.num_second_pass
            total_second_necessary += len(res.second_pass)
            for fq in res.fq_list:
                fq_all.append(fq)
                fq_summary_lines.append(
                    f"- [{slot_name}] {fq.get('query_text')} → "
                    f"{fq.get('target_source_ids', [])}"
                )
            verify_pins.append({
                "slot": slot_name, "method": res.method,
                "num_first_pass": res.num_first_pass,
                "num_necessary": len(res.necessary),
                "num_multihop": len(res.multihop_ids),
                "rationale_present": bool(res.rationale),
                "num_second_pass": res.num_second_pass,
                "num_second_necessary": len(res.second_pass),
                "second_method": res.second_method,
            })
            # 슬롯 검증 근거 라인(1차 검증) — 카운트 + Node1 rationale. method=fallback
            # 은 검증 LLM 호출 실패(또는 도구 실패) → 그 슬롯 청크 전량 보존이므로 실패
            # 마커를 붙여 UI thinking 에 명시한다(adapter 가 채운 rationale 도 근거로 노출).
            line = (f"- [{slot_name}] 1차 {res.num_first_pass}개 → 필요 "
                    f"{len(res.necessary)}개, 멀티홉 {len(res.multihop_ids)}개")
            if res.method == "fallback":
                line += " ⚠ 검증 호출 실패 → 전량 보존"
            if res.rationale:
                line += f"\n    근거: {res.rationale}"
            verify_reason_lines.append(line)
            # 2차 재검증 근거 라인(Stage 4) — 2차 검색이 있었던 슬롯만.
            if res.num_second_pass > 0:
                sline = (f"  · 2차 검색 {res.num_second_pass}개 → 채택 "
                         f"{len(res.second_pass)}개")
                if res.second_method == "fallback":
                    sline += " ⚠ 검증 호출 실패 → 전량 보존"
                if res.rationale2:
                    sline += f"\n    근거: {res.rationale2}"
                verify_reason_lines.append(sline)

        # 전역 집계 속성 — 버킷 span 제거 후 현재 span(=base run() 의 agent.run)에 단다.
        run_span = trace.get_current_span()
        run_span.set_attribute("verify.num_slots", len(slot_order))
        run_span.set_attribute("verify.total_necessary", len(necessary_by_id))
        run_span.set_attribute("verify.total_multihop", total_multihop)
        run_span.set_attribute("verify.added_second_pass", len(second_by_id))
        run_span.set_attribute("verify.second_pass_total", total_second_pass)
        run_span.set_attribute("verify.second_necessary_total", total_second_necessary)
        run_span.set_attribute("follow_up.num_queries", len(fq_all))
        run_span.set_attribute("follow_up.num_slots_multihop", slots_with_multihop)
        run_span.set_attribute("second_search.total", total_second_pass)
        run_span.set_attribute("second_search.added", len(second_by_id))
        await emit_step("slot_verify", "ok",
                        necessary=len(necessary_by_id),
                        multihop=total_multihop,
                        second_pass=total_second_pass,
                        added_chunks=len(second_by_id))

        # === N4 컨텍스트 trim — 필요 청크 ∪ 2차 결과만(1차 전량 보존 폐기, 결정 #4) ===
        # necessary 를 always-include(1차 자리), 2차를 score 순 채움. _assemble_final_chunks
        # 의 first_pass_ids 자리에 necessary_ids 를 넣어 동일 토큰 예산 거버너를 재사용한다.
        necessary_ids = set(necessary_by_id)
        # merged 재구성 — necessary ∪ second 만(1차 비필요 청크는 제외 → trim).
        kept_by_id = {**necessary_by_id, **second_by_id}
        v2_merged = sorted(kept_by_id.values(), key=lambda c: c.score, reverse=True)
        final_chunks, budget_log, total_tokens_est, necessary_dropped = (
            _assemble_final_chunks(necessary_ids, v2_merged, self._context_token_budget)
        )
        evidence_gap = not final_chunks
        if budget_log:
            await emit_step("context_budget", "ok",
                            budget=self._context_token_budget,
                            total_tokens_est=total_tokens_est,
                            dropped=len(budget_log),
                            necessary_dropped=necessary_dropped)

        fq_summary = "\n".join(fq_summary_lines) if fq_summary_lines else None
        # UI thinking — Node1 슬롯 검증/재검증 근거 블록(B2). base run() 이 단계 종료 후
        # emit_reasoning 으로 한 번 방출한다(fq_summary 앞).
        extra_reasoning = (
            "\n**슬롯 검증 (Node1)**\n" + "\n".join(verify_reason_lines) + "\n"
            if verify_reason_lines else None
        )

        node1_id = self._llm_router.default_id if self._llm_router else ""
        try:
            _, _resolved = self._llm_router.resolve(request.model or None)
            node1_id = request.model or node1_id
        except Exception:  # noqa: BLE001
            pass

        qu_sections: dict[str, Any] = {
            "node1_llm_id": node1_id,
            "retrieval": {
                "num_chunks": len(final_chunks),
                "merged": len(v2_merged),
                "budget": self._max_context_chunks,
                "fetch_k": per_query_k,
                # v2 는 1차 전량 보존을 폐기 — Node1 이 고른 필요 청크 수가 always-include 기준.
                "necessary_kept": len(necessary_ids),
                "first_pass_total": len(first_pass_ids),
                "per_query_counts": per_query_counts,
                "min_token_count": self._min_token_count,
                "filters": dict(_NOISE_FILTER),
                "floored_slots": coverage["floored_slots"],
                "covered_required_slots": coverage["covered_required"],
                "uncovered_required_slots": coverage["uncovered_required"],
            },
            "verify": {
                "node1": True,
                "num_slots": len(slot_order),
                "total_necessary": len(necessary_ids),
                "total_multihop": total_multihop,
                "added_second_pass": len(second_by_id),
                # Stage 4 — 2차 검색 결과 Node1 재검증(검색 후 항상 relevance).
                "second_pass_total": total_second_pass,
                "second_necessary_total": total_second_necessary,
                "slots": verify_pins,
            },
            "follow_up": {
                "necessity_only": True,
                "num_queries": len(fq_all),
                "added_chunks": len(second_by_id),
                "queries": [
                    {
                        "query_text": fq.get("query_text"),
                        "target_source_ids": fq.get("target_source_ids", []),
                        "intent": fq.get("intent"),
                    }
                    for fq in fq_all
                ],
            },
            "context_budget": {
                "budget": self._context_token_budget,
                "total_tokens_est": total_tokens_est,
                "dropped_chunk_ids": budget_log,
                # v2 는 1차가 아니라 necessary 가 always-include 기준 → 이름도 그에 맞춘다.
                "necessary_dropped": necessary_dropped,
            },
        }
        return _PostRetrievalOutcome(
            chunks=final_chunks, evidence_gap=evidence_gap, qu_sections=qu_sections,
            fq_summary=fq_summary, source_ids_fq=fq_all,
            extra_reasoning=extra_reasoning,
        )

    async def _run_slot_pipeline(
        self, *, request, ctx: ToolExecutionContext, spec_block: str,
        slot_name: str, slot_query: str, slot_chunks: list[RetrievedChunk],
    ) -> _SlotPipelineResult:
        """한 슬롯: Node1 검증 → (멀티홉 청크에 대해) Node2 외부참조 선별 → 2차 검색.
        tool 결과는 record 하지 않고 모아서 반환한다(병합 단계가 슬롯 원순서로 순차 record →
        결정성·race 방지). 어떤 단계든 실패/미배선이면 안전 degrade(necessary=전량, 멀티홉
        없음 → 단일노드 동작과 동형).

        슬롯의 4 stage 도구 호출은 여기서 연 `agent.slot.<name>` span 하나의 자식으로 묶인다
        — gather task 마다 contextvars 가 독립이라 슬롯끼리 섞이지 않는다(Phoenix 에서
        agent.run > agent.slot.<name> > {도구} 로 슬롯 단위로 읽힌다)."""
        tool_results: list[Any] = []
        if not slot_chunks:
            # 0건 슬롯 — verify 호출 낭비 방지(빈 기여). span 도 열지 않는다.
            return _SlotPipelineResult(
                slot_name=slot_name, method="empty", num_first_pass=0,
                necessary=[], multihop_ids=[], rationale="",
            )

        with _TRACER.start_as_current_span(f"agent.slot.{slot_name}") as slot_span:
            oi.set_kind(slot_span, oi.KIND_CHAIN)
            oi.set_io(slot_span, input_value=slot_query)
            slot_span.set_attribute("slot.name", slot_name)
            slot_span.set_attribute("slot.num_first_pass", len(slot_chunks))

            by_id = {c.chunk_id: c for c in slot_chunks}
            # === Stage 1 — Node1 verify ===
            method = "fallback"
            necessary_ids: list[str] = list(by_id)   # fallback 기본 = 전량 필요
            multihop_ids: list[str] = []
            rationale = ""
            try:
                v = await self._tools.invoke(
                    _VERIFY_TOOL,
                    {
                        "query_text": request.query_text,
                        "answer_spec": spec_block,
                        "slot_name": slot_name,
                        "slot_query": slot_query,
                        "chunks": [c.model_dump(mode="json") for c in slot_chunks],
                    },
                    ctx,
                )
                tool_results.append(v)
                if v.status == "success" and v.output:
                    method = str(v.output.get("method", "llm"))
                    nids = [i for i in (v.output.get("necessary_chunk_ids") or []) if i in by_id]
                    mids = [i for i in (v.output.get("multihop_chunk_ids") or []) if i in by_id]
                    necessary_ids = nids
                    multihop_ids = mids
                    rationale = str(v.output.get("rationale", ""))
            except Exception:  # noqa: BLE001 — ToolUnknown/실패 → 단일노드 degrade(전량 필요).
                method = "fallback"
                necessary_ids = list(by_id)
                multihop_ids = []
                # adapter fallback 과 일관 — 빈 근거 대신 실패 사유를 실어 UI thinking 노출.
                rationale = "⚠ 검증 도구 호출 실패 → 이 슬롯 청크 전량 보존"

            necessary = [by_id[i] for i in necessary_ids if i in by_id]
            multihop = [by_id[i] for i in multihop_ids if i in by_id]

            # === Stage 2 — Node2 외부참조 선별(enhanced follow_up, necessity_only) ===
            fq_list: list[dict[str, Any]] = []
            if multihop:
                try:
                    fu = await self._tools.invoke(
                        _FOLLOW_UP_TOOL,
                        {
                            "query_text": request.query_text,
                            "chunks": [c.model_dump(mode="json") for c in multihop],
                            "answer_spec": spec_block,
                            "slot_query": slot_query,
                            "necessity_only": True,
                        },
                        ctx,
                    )
                    tool_results.append(fu)
                    if fu.status == "success" and fu.output:
                        fq_list = fu.output.get("follow_up_queries", []) or []
                except Exception:  # noqa: BLE001 — graceful skip(2차 검색 없음).
                    fq_list = []

            # === Stage 3 — Node2 2차 검색(참조 문서 내부, score 게이트 keep_k) ===
            second_pass_raw: list[RetrievedChunk] = []
            searchable = [fq for fq in fq_list if fq.get("target_source_ids")]
            if searchable:
                # gather 의 자식 search span 들은 task 생성 시점 context(=이 슬롯 span)를
                # 캡처하므로 모두 슬롯 span 의 자식으로 nesting 된다.
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
                seen: set[str] = set()
                for sub_res in sub_results:
                    if isinstance(sub_res, BaseException):
                        continue
                    tool_results.append(sub_res)
                    found = _parse_chunks(
                        sub_res.output if sub_res.status == "success" else None
                    )
                    for c in found[: self._follow_up_keep_k]:
                        if c.chunk_id not in by_id and c.chunk_id not in seen:
                            seen.add(c.chunk_id)
                            second_pass_raw.append(c)

            # === Stage 4 — 2차 검색 결과 Node1 재검증("검색 후엔 항상 relevance") ===
            # 2차 청크를 동일 retrieval.verify_slot 도구로 한 번 더 검증해 *답변에 필요한* 청크만
            # N4 로 보낸다. 멀티홉 출력은 무시한다(3차 홉 없음 — 두번째 Node1 호출 후 바로 N4).
            # 미배선/실패/fallback → 2차 전량 보존(단일노드 degrade). 빈 2차는 스킵(호출 낭비 방지).
            second_pass = second_pass_raw
            num_second_pass = len(second_pass_raw)
            second_method = "skip"
            rationale2 = ""
            if second_pass_raw:
                sp_by_id = {c.chunk_id: c for c in second_pass_raw}
                second_method = "fallback"  # 기본 = 검증 실패 시 전량 보존
                try:
                    v2 = await self._tools.invoke(
                        _VERIFY_TOOL,
                        {
                            "query_text": request.query_text,
                            "answer_spec": spec_block,
                            "slot_name": slot_name,
                            "slot_query": slot_query,
                            "chunks": [c.model_dump(mode="json") for c in second_pass_raw],
                        },
                        ctx,
                    )
                    tool_results.append(v2)
                    if v2.status == "success" and v2.output:
                        second_method = str(v2.output.get("method", "llm"))
                        keep = [i for i in (v2.output.get("necessary_chunk_ids") or [])
                                if i in sp_by_id]
                        rationale2 = str(v2.output.get("rationale", ""))
                        if second_method != "fallback":
                            # 검증 통과 청크만 보존(원순서 유지). fallback 이면 전량 유지.
                            second_pass = [sp_by_id[i] for i in keep if i in sp_by_id]
                except Exception:  # noqa: BLE001 — ToolUnknown/실패 → 2차 전량 보존(degrade).
                    second_method = "fallback"
                    second_pass = second_pass_raw
                    # adapter fallback 과 일관 — 빈 근거 대신 실패 사유를 실어 UI thinking 노출.
                    rationale2 = "⚠ 검증 도구 호출 실패 → 2차 검색 청크 전량 보존"

            # slot span output — CHAIN span 은 자식 처리 후 자기 output 을 구조화 summary 로
            # 단다(intake 노드 패턴). 미설정 시 Phoenix 가 자식 LLM 의 assistant 메시지를
            # 끌어와 표시하므로, 슬롯 처리 결과 요약을 명시한다(verify_pins 와 동일 출처).
            oi.set_io(slot_span, output_value={
                "method": method,
                "num_necessary": len(necessary),
                "num_multihop": len(multihop_ids),
                "num_second_pass": num_second_pass,
                "num_second_necessary": len(second_pass),
                "second_method": second_method,
                "rationale": rationale,
            })

        return _SlotPipelineResult(
            slot_name=slot_name, method=method, num_first_pass=len(slot_chunks),
            necessary=necessary, multihop_ids=multihop_ids, rationale=rationale,
            fq_list=fq_list, second_pass=second_pass, tool_results=tool_results,
            num_second_pass=num_second_pass, second_method=second_method,
            rationale2=rationale2,
        )


@register_variant(SPEC_DRIVEN_V2_VARIANT_ID)
def _build_spec_driven_v2(spec: VariantSpec, deps: AgentDeps) -> "SpecDrivenV2Runner":
    """spec_driven_v2 팩토리 — `_build_spec_driven`(v1) 미러. v2 전용 프롬프트 source
    (deps.spec_driven_v2_*)를 주입하되, 미배선이면 v1 source 로 폴백(부트 누락 방어).
    Node1 검증은 retrieval.verify_slot 도구(profiles.py 가 utility_llm 에 배선)로
    호출하므로 러너에 LLM 을 직접 주입하지 않는다(D1 — 검증은 도구 안에만 존재)."""
    t = deps.tunables
    return SpecDrivenV2Runner(
        verify_concurrency=t.get("spec_driven_v2_verify_concurrency", 10),
        spec=spec,
        llm_router=deps.llm_router,
        tool_executor=deps.tool_executor,
        context_builder=deps.context_builder,
        recorder=deps.recorder,
        event_sink=deps.event_sink,
        app_profile=deps.app_profile,
        utility_llm=deps.utility_llm,
        answer_spec_source=(
            deps.spec_driven_v2_answer_spec_source
            or deps.spec_driven_answer_spec_source
        ),
        query_source=deps.spec_driven_v2_query_source or deps.spec_driven_query_source,
        generation_source=(
            deps.spec_driven_v2_generation_source
            or deps.spec_driven_generation_source
        ),
        triage_source=deps.spec_driven_v2_triage_source or deps.spec_driven_triage_source,
        general_source=deps.spec_driven_v2_general_source or deps.spec_driven_general_source,
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
