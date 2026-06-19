"""멀티홉 2차 재검증의 *쿼리 그룹별* 분리 — `_run_slot_pipeline` Stage 3/4 단위 검증.

설계: docs/plans (멀티홉 2차 검증을 쿼리 그룹별로 분리). 검증 포인트:
  - Stage 3 가 2차 검색 결과를 평탄화하지 않고 *쿼리(fq)별 그룹* 으로 묶는다(서로소).
  - Stage 4 가 그룹마다 retrieval.verify_slot 을 1회씩(멀티홉 N갈래 → N회) 호출하고,
    각 호출의 `slot_query` 에 *그 그룹의 검색 맥락*(원 슬롯 요구 + 재검색 쿼리 + 다리 근거)을
    싣는다(`_second_pass_verify_context`).
  - 통과 청크 union(by_id, 순서보존), 한 그룹 fallback 시 그 그룹만 전량 보존.

컨테이너 불필요 — 도메인 모델 + 가짜 ToolExecutor 로 믹스인을 직접 돌린다."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.application.agents.slot_pipeline import (
    _SlotPipelineMixin,
    _second_pass_verify_context,
)
from app.domain.retrieval import RetrievedChunk
from app.ports.tool import ToolExecutionContext

_VERIFY = "retrieval.verify_slot"
_FOLLOW_UP = "retrieval.follow_up"
_SEARCH = "retrieval.search"


@dataclass
class _FakeToolResult:
    status: str
    output: dict[str, Any] | None


class _CapturingTools:
    """가짜 ToolExecutor — tool 이름별 핸들러로 디스패치하고 모든 호출을 기록한다.

    `verify_calls` 에 verify_slot 호출의 (slot_query, chunk_ids) 를 순서대로 담아
    그룹별 호출 검증에 쓴다. 검색은 fq query_text → 청크 매핑(search_map)으로 응답."""

    def __init__(self, *, search_map: dict[str, list[RetrievedChunk]],
                 verify_keep: dict[str, list[str]] | None = None,
                 verify_fail_for: set[str] | None = None) -> None:
        self._search_map = search_map
        self._verify_keep = verify_keep or {}
        self._verify_fail_for = verify_fail_for or set()
        self.verify_calls: list[dict[str, Any]] = []
        self.follow_up_calls: list[dict[str, Any]] = []

    async def invoke(self, name: str, payload: dict[str, Any],
                     ctx: ToolExecutionContext) -> _FakeToolResult:
        if name == _VERIFY:
            ids = [c["chunk_id"] for c in payload["chunks"]]
            self.verify_calls.append({"slot_query": payload["slot_query"], "ids": list(ids)})
            # 1차 검증(Stage1)은 전량 necessary + 멀티홉으로 표시(아래 _first_pass 로 식별).
            # Stage4 그룹 검증은 verify_keep 매핑으로 통과 청크를 좁힌다.
            key = "|".join(ids)
            if key in self._verify_fail_for:
                return _FakeToolResult("success", {
                    "necessary_chunk_ids": ids, "method": "fallback",
                    "rationale": "fallback 전량 보존"})
            keep = self._verify_keep.get(key, ids)
            return _FakeToolResult("success", {
                "necessary_chunk_ids": keep, "multihop_chunk_ids": [],
                "rationale": f"keep {keep}", "method": "llm"})
        if name == _FOLLOW_UP:
            self.follow_up_calls.append(dict(payload))
            return _FakeToolResult("success", {"follow_up_queries": self._follow_up_queries})
        if name == _SEARCH:
            qt = payload.get("query_text", "")
            return _FakeToolResult("success", {
                "chunks": [c.model_dump(mode="json") for c in self._search_map.get(qt, [])]})
        raise AssertionError(f"unexpected tool {name}")

    _follow_up_queries: list[dict[str, Any]] = []


class _Host(_SlotPipelineMixin):
    """믹스인 호스트 — `_run_slot_pipeline` 이 요구하는 속성만 채운다."""

    def __init__(self, tools: _CapturingTools) -> None:
        self._tools = tools
        self._follow_up_fetch_k = 10
        self._follow_up_keep_k = 5
        self._min_token_count = 0


@dataclass
class _Req:
    query_text: str = "원본 질문"


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="i", trace_id="t", app_profile="local", agent_variant="v")


def _chunk(cid: str, *, score: float = 0.5, body: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(chunk_id=cid, document_id=cid, score=score,
                          snippet=body or f"body {cid}")


# --------------------------------------------------------------------------
# _second_pass_verify_context — 순수 함수
# --------------------------------------------------------------------------
def test_context_followup_uses_intent_as_bridge() -> None:
    """follow_up 경로 — intent 가 '검색 근거' 로 실리고 원 슬롯 요구/재검색 쿼리 포함."""
    out = _second_pass_verify_context(
        slot_query="원 슬롯 요구", research_mode=False,
        what_is_needed="",
        fq={"query_text": "RG 1.68 acceptance limits", "intent": "preop 시험 수치 한계"})
    assert "[원 슬롯 요구] 원 슬롯 요구" in out
    assert "재검색 쿼리: RG 1.68 acceptance limits" in out
    assert "검색 근거: preop 시험 수치 한계" in out
    # intent 가 이미 검색 근거로 쓰였으면 중복 '의도:' 줄을 또 달지 않는다.
    assert "의도:" not in out


def test_context_rescope_uses_what_is_needed() -> None:
    """rescope 경로 — what_is_needed 가 '검색 근거', intent 가 따로 있으면 '의도:' 로 덧붙음."""
    out = _second_pass_verify_context(
        slot_query="원 슬롯", research_mode=True,
        what_is_needed="NuScale FSAR ECCS design",
        fq={"query_text": "passive ECCS design", "intent": "설계 세부"})
    assert "검색 근거: NuScale FSAR ECCS design" in out
    assert "의도: 설계 세부" in out


def test_context_omits_empty_rationale() -> None:
    """근거/의도 모두 비면 그 줄을 생략(결정형)."""
    out = _second_pass_verify_context(
        slot_query="q", research_mode=False, what_is_needed="",
        fq={"query_text": "qt", "intent": ""})
    assert out == "[원 슬롯 요구] q\n재검색 쿼리: qt"


# --------------------------------------------------------------------------
# Stage 3/4 — 멀티홉 2갈래 그룹별 검증
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multihop_two_branches_verify_per_group() -> None:
    """멀티홉 2갈래 → 2차 검색 2회 + Stage4 verify 그룹당 1회(총 2회, Stage1 포함 3회).
    각 그룹 verify 의 slot_query 에 해당 그룹의 재검색 쿼리/근거가 실린다."""
    mh = _chunk("m1")  # 1차 멀티홉 청크
    a1, a2 = _chunk("a1", score=0.9), _chunk("a2", score=0.8)  # 그룹 A 2차
    b1 = _chunk("b1", score=0.7)                               # 그룹 B 2차
    tools = _CapturingTools(search_map={
        "qa": [a1, a2], "qb": [b1],
    })
    # follow_up 이 2갈래 쿼리를 낸다(intent 포함).
    tools._follow_up_queries = [
        {"query_text": "qa", "target_source_ids": ["SA"], "intent": "근거 A"},
        {"query_text": "qb", "target_source_ids": ["SB"], "intent": "근거 B"},
    ]
    # Stage1 verify: m1 을 멀티홉으로 표시. Stage4 그룹: 전량 통과.
    # Stage1 호출의 chunk = [m1] → multihop 으로 응답하도록 verify_keep 외 별도 처리.
    # _CapturingTools 는 necessary=전량/멀티홉없음이 기본이므로 Stage1 멀티홉을 위해
    # output 을 직접 조정: m1 단독 입력일 때 multihop 으로.
    orig_invoke = tools.invoke

    async def invoke(name, payload, ctx):  # noqa: ANN001
        if name == _VERIFY and [c["chunk_id"] for c in payload["chunks"]] == ["m1"]:
            tools.verify_calls.append({"slot_query": payload["slot_query"], "ids": ["m1"]})
            return _FakeToolResult("success", {
                "necessary_chunk_ids": ["m1"], "multihop_chunk_ids": ["m1"],
                "multihop_search_directions": {"m1": "외부문서에서 X 를 찾아라"},
                "rationale": "멀티홉", "method": "llm"})
        return await orig_invoke(name, payload, ctx)

    tools.invoke = invoke  # type: ignore[method-assign]

    host = _Host(tools)
    res = await host._run_slot_pipeline(
        request=_Req(), ctx=_ctx(), spec_block="SPEC",
        slot_name="slotX", slot_query="원 슬롯 요구", slot_chunks=[mh])

    # Stage4 그룹별 verify — 정확히 2회(Stage1 1회 제외하고 2차 검증 2회).
    group_calls = [c for c in tools.verify_calls if c["ids"] != ["m1"]]
    assert len(group_calls) == 2
    by_ids = {tuple(c["ids"]): c for c in group_calls}
    assert ("a1", "a2") in by_ids and ("b1",) in by_ids
    # 각 그룹 호출의 slot_query 에 자기 재검색 쿼리/근거가 실렸다.
    assert "재검색 쿼리: qa" in by_ids[("a1", "a2")]["slot_query"]
    assert "검색 근거: 근거 A" in by_ids[("a1", "a2")]["slot_query"]
    assert "재검색 쿼리: qb" in by_ids[("b1",)]["slot_query"]
    assert "검색 근거: 근거 B" in by_ids[("b1",)]["slot_query"]
    # 두 그룹 통과 청크 union — a1,a2,b1 전부.
    assert {c.chunk_id for c in res.second_pass} == {"a1", "a2", "b1"}
    assert res.num_second_pass == 3
    assert res.second_method == "llm"


@pytest.mark.asyncio
async def test_groups_are_disjoint_same_chunk_once() -> None:
    """두 쿼리가 같은 청크를 반환해도 처음 찾은 그룹에만 귀속(서로소)."""
    mh = _chunk("m1")
    shared = _chunk("s1", score=0.9)
    only_b = _chunk("s2", score=0.6)
    tools = _CapturingTools(search_map={"qa": [shared], "qb": [shared, only_b]})
    tools._follow_up_queries = [
        {"query_text": "qa", "target_source_ids": ["SA"], "intent": "A"},
        {"query_text": "qb", "target_source_ids": ["SB"], "intent": "B"},
    ]
    orig = tools.invoke

    async def invoke(name, payload, ctx):  # noqa: ANN001
        if name == _VERIFY and [c["chunk_id"] for c in payload["chunks"]] == ["m1"]:
            tools.verify_calls.append({"slot_query": payload["slot_query"], "ids": ["m1"]})
            return _FakeToolResult("success", {
                "necessary_chunk_ids": ["m1"], "multihop_chunk_ids": ["m1"],
                "multihop_search_directions": {"m1": "X"}, "method": "llm", "rationale": ""})
        return await orig(name, payload, ctx)

    tools.invoke = invoke  # type: ignore[method-assign]
    host = _Host(tools)
    res = await host._run_slot_pipeline(
        request=_Req(), ctx=_ctx(), spec_block="S",
        slot_name="slotX", slot_query="q", slot_chunks=[mh])

    group_calls = [c for c in tools.verify_calls if c["ids"] != ["m1"]]
    # 그룹 A = [s1], 그룹 B = [s2](s1 은 이미 A 에 귀속 → B 에서 제외).
    id_sets = sorted(tuple(c["ids"]) for c in group_calls)
    assert id_sets == [("s1",), ("s2",)]
    assert {c.chunk_id for c in res.second_pass} == {"s1", "s2"}


@pytest.mark.asyncio
async def test_one_group_fallback_preserves_that_group_only() -> None:
    """한 그룹 verify 가 fallback → 그 그룹은 전량 보존, 다른 그룹은 정상 trim."""
    mh = _chunk("m1")
    a1, a2 = _chunk("a1"), _chunk("a2")
    b1, b2 = _chunk("b1"), _chunk("b2")
    tools = _CapturingTools(
        search_map={"qa": [a1, a2], "qb": [b1, b2]},
        # 그룹 A 는 a1 만 통과로 trim, 그룹 B 는 fallback(전량 보존).
        verify_keep={"a1|a2": ["a1"]},
        verify_fail_for={"b1|b2"},
    )
    tools._follow_up_queries = [
        {"query_text": "qa", "target_source_ids": ["SA"], "intent": "A"},
        {"query_text": "qb", "target_source_ids": ["SB"], "intent": "B"},
    ]
    orig = tools.invoke

    async def invoke(name, payload, ctx):  # noqa: ANN001
        if name == _VERIFY and [c["chunk_id"] for c in payload["chunks"]] == ["m1"]:
            tools.verify_calls.append({"slot_query": payload["slot_query"], "ids": ["m1"]})
            return _FakeToolResult("success", {
                "necessary_chunk_ids": ["m1"], "multihop_chunk_ids": ["m1"],
                "multihop_search_directions": {"m1": "X"}, "method": "llm", "rationale": ""})
        return await orig(name, payload, ctx)

    tools.invoke = invoke  # type: ignore[method-assign]
    host = _Host(tools)
    res = await host._run_slot_pipeline(
        request=_Req(), ctx=_ctx(), spec_block="S",
        slot_name="slotX", slot_query="q", slot_chunks=[mh])

    # 그룹 A trim → a1 만, 그룹 B fallback → b1,b2 전량.
    assert {c.chunk_id for c in res.second_pass} == {"a1", "b1", "b2"}
    # 하나라도 llm 검증이 돌면 method=llm(전량-보존은 그 그룹에 국한, 전체가 degrade 는 아님).
    assert res.second_method == "llm"
    assert res.num_second_pass == 4  # 검증 입력 수(a1,a2,b1,b2)


@pytest.mark.asyncio
async def test_all_groups_fallback_method_is_fallback() -> None:
    """모든 그룹 verify 가 fallback → second_method=fallback, 전 그룹 전량 보존."""
    mh = _chunk("m1")
    a1, b1 = _chunk("a1"), _chunk("b1")
    tools = _CapturingTools(
        search_map={"qa": [a1], "qb": [b1]},
        verify_fail_for={"a1", "b1"},
    )
    tools._follow_up_queries = [
        {"query_text": "qa", "target_source_ids": ["SA"], "intent": "A"},
        {"query_text": "qb", "target_source_ids": ["SB"], "intent": "B"},
    ]
    orig = tools.invoke

    async def invoke(name, payload, ctx):  # noqa: ANN001
        if name == _VERIFY and [c["chunk_id"] for c in payload["chunks"]] == ["m1"]:
            tools.verify_calls.append({"slot_query": payload["slot_query"], "ids": ["m1"]})
            return _FakeToolResult("success", {
                "necessary_chunk_ids": ["m1"], "multihop_chunk_ids": ["m1"],
                "multihop_search_directions": {"m1": "X"}, "method": "llm", "rationale": ""})
        return await orig(name, payload, ctx)

    tools.invoke = invoke  # type: ignore[method-assign]
    host = _Host(tools)
    res = await host._run_slot_pipeline(
        request=_Req(), ctx=_ctx(), spec_block="S",
        slot_name="slotX", slot_query="q", slot_chunks=[mh])
    assert {c.chunk_id for c in res.second_pass} == {"a1", "b1"}
    assert res.second_method == "fallback"
