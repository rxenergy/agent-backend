from __future__ import annotations

import pytest

from app.application.retrieval.dispatcher import RetrievalDispatcher
from app.application.tool_runtime.errors import RequiredToolFailed
from app.application.tool_runtime.executor import ToolErrorCode
from app.domain.retrieval import (
    RetrievalPlan,
    RetrievalStrategy,
    RetrievedChunk,
    RetrieverSearchInput,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="i", trace_id="t", app_profile="local",
        agent_variant="hierarchical_corrective_v3_1",
    )


def _plan(*strategies: str) -> RetrievalPlan:
    return RetrievalPlan(
        rule_id="r", strategies=tuple(RetrievalStrategy(name=s) for s in strategies),
    )


class _StrategyVaryingExecutor:
    """Fake ToolExecutor whose chunks DIFFER per strategy — so a dispatcher bug
    that ignores the plan / calls one strategy N times would collapse RRF to a
    single list and fail these assertions (advisor: strategy-blind fake hides
    that class of bug)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, name, tool_input, context) -> ToolResult:
        ti = RetrieverSearchInput.model_validate(tool_input)
        self.calls.append(ti.strategy)
        # Each strategy surfaces a distinct top chunk + a shared one ("common").
        top = f"{ti.strategy}-top"
        chunks = [
            RetrievedChunk(chunk_id=top, document_id="d", score=0.9),
            RetrievedChunk(chunk_id="common", document_id="d", score=0.5),
        ]
        out = RetrieverSearchOutput(chunks=chunks)
        return ToolResult(
            tool_name=name, tool_version="v1", status="success",
            output=out.model_dump(mode="json"), latency_ms=0,
            input_hash=f"hash-{ti.strategy}", trace_id=context.trace_id,
        )


class _PartialFailExecutor:
    """bm25 succeeds, vector raises RequiredToolFailed."""

    async def invoke(self, name, tool_input, context) -> ToolResult:
        ti = RetrieverSearchInput.model_validate(tool_input)
        if ti.strategy == "vector":
            raise RequiredToolFailed(name, ToolErrorCode.INTERNAL_ERROR, "boom")
        out = RetrieverSearchOutput(
            chunks=[RetrievedChunk(chunk_id="bm25-top", document_id="d", score=0.8)]
        )
        return ToolResult(
            tool_name=name, tool_version="v1", status="success",
            output=out.model_dump(mode="json"), latency_ms=0,
            input_hash="h", trace_id=context.trace_id,
        )


class _AllFailExecutor:
    async def invoke(self, name, tool_input, context) -> ToolResult:
        raise RequiredToolFailed(name, ToolErrorCode.INTERNAL_ERROR, "down")


@pytest.mark.asyncio
async def test_dispatch_fans_out_each_strategy_and_fuses():
    ex = _StrategyVaryingExecutor()
    disp = RetrievalDispatcher(ex, rrf_k=60)
    res = await disp.execute(
        _plan("hybrid", "bm25"),
        query_text="q", fetch_k=10, scenario_object="O1", scenario_depth="D1",
        entities={}, ctx=_ctx(),
    )
    # Each strategy was called exactly once with its own strategy arg.
    assert sorted(ex.calls) == ["bm25", "hybrid"]
    ids = {c.chunk_id for c in res.fused_chunks}
    # Distinct per-strategy tops both present + the shared chunk → proves
    # distinct lists were actually fused (not one list reused).
    assert {"hybrid-top", "bm25-top", "common"} == ids
    # 'common' appears in both → ranked above the single-strategy tops.
    assert res.fused_chunks[0].chunk_id == "common"
    assert [s.name for s in res.executed] == ["hybrid", "bm25"]
    assert res.executed[0].args_hash == "hash-hybrid"
    assert len(res.tool_results) == 2


@pytest.mark.asyncio
async def test_dispatch_tolerates_partial_failure():
    disp = RetrievalDispatcher(_PartialFailExecutor(), rrf_k=60)
    res = await disp.execute(
        _plan("bm25", "vector"),
        query_text="q", fetch_k=10, scenario_object=None, scenario_depth=None,
        entities={}, ctx=_ctx(),
    )
    assert [c.chunk_id for c in res.fused_chunks] == ["bm25-top"]
    assert res.failed_strategies == ["vector"]
    assert [s.name for s in res.executed] == ["bm25"]


class _InputCapturingExecutor:
    """검색 입력을 전부 기록 — scope(target/filters/min_token_count) passthrough 검증."""

    def __init__(self) -> None:
        self.inputs: list[RetrieverSearchInput] = []

    async def invoke(self, name, tool_input, context) -> ToolResult:
        ti = RetrieverSearchInput.model_validate(tool_input)
        self.inputs.append(ti)
        out = RetrieverSearchOutput(
            chunks=[RetrievedChunk(chunk_id=f"{ti.strategy}-c", document_id="d", score=0.7)]
        )
        return ToolResult(
            tool_name=name, tool_version="v1", status="success",
            output=out.model_dump(mode="json"), latency_ms=0,
            input_hash="h", trace_id=context.trace_id,
        )


@pytest.mark.asyncio
async def test_dispatch_threads_scope_into_every_leg():
    ex = _InputCapturingExecutor()
    disp = RetrievalDispatcher(ex, rrf_k=60)
    await disp.execute(
        _plan("hybrid", "bm25"),
        query_text="q", fetch_k=10, scenario_object="O1", scenario_depth="D1",
        entities={}, ctx=_ctx(),
        target={"collection": ["SRP"]},
        filters={"collection": ["10CFR"], "search_type": "manual"},
        min_token_count=12,
    )
    assert len(ex.inputs) == 2  # 전 strategy leg 에 공통 전달
    for ti in ex.inputs:
        assert ti.target == {"collection": ["SRP"]}
        assert ti.filters == {"collection": ["10CFR"], "search_type": "manual"}
        assert ti.min_token_count == 12


@pytest.mark.asyncio
async def test_dispatch_scope_defaults_are_empty():
    ex = _InputCapturingExecutor()
    disp = RetrievalDispatcher(ex, rrf_k=60)
    await disp.execute(
        _plan("hybrid"),
        query_text="q", fetch_k=5, scenario_object=None, scenario_depth=None,
        entities={}, ctx=_ctx(),
    )
    ti = ex.inputs[0]
    assert ti.target == {} and ti.filters == {} and ti.min_token_count == 0


@pytest.mark.asyncio
async def test_dispatch_all_fail_reraises():
    disp = RetrievalDispatcher(_AllFailExecutor(), rrf_k=60)
    with pytest.raises(RequiredToolFailed):
        await disp.execute(
            _plan("hybrid", "bm25"),
            query_text="q", fetch_k=10, scenario_object=None, scenario_depth=None,
            entities={}, ctx=_ctx(),
        )
