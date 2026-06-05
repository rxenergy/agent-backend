from __future__ import annotations

import pytest

from app.application.retrieval.dispatcher import RetrievalDispatcher
from app.application.tool_runtime.errors import RequiredToolFailed
from app.application.tool_runtime.executor import ToolErrorCode
from app.domain.retrieval import (
    RerankInput,
    RerankOutput,
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


def _ok(name: str, output: dict, *, input_hash: str = "h", ctx=None) -> ToolResult:
    return ToolResult(
        tool_name=name, tool_version="v1", status="success",
        output=output, latency_ms=0, input_hash=input_hash,
        trace_id=ctx.trace_id if ctx else "t",
    )


def _rerank_by_chunk_id_desc(tool_input, context) -> ToolResult:
    """Fake reranker that orders candidates by chunk_id DESC (distinct from raw
    score / search order) — so a dispatcher bug that skips rerank and keeps the
    union order would fail the order assertions."""
    ti = RerankInput.model_validate(tool_input)
    ranked = sorted(ti.candidates, key=lambda c: c.chunk_id, reverse=True)
    scores = {c.chunk_id: float(i + 1) for i, c in enumerate(reversed(ranked))}
    out = RerankOutput(chunks=ranked, scores=scores)
    return _ok("retriever.rerank", out.model_dump(mode="json"), ctx=context)


class _StrategyVaryingExecutor:
    """Fake ToolExecutor whose search chunks DIFFER per strategy, plus a reranker
    leg. A dispatcher bug that ignores the plan / drops a strategy would lose a
    chunk from the union and fail these assertions."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rerank_calls = 0

    async def invoke(self, name, tool_input, context) -> ToolResult:
        if name == "retriever.rerank":
            self.rerank_calls += 1
            return _rerank_by_chunk_id_desc(tool_input, context)
        ti = RetrieverSearchInput.model_validate(tool_input)
        self.calls.append(ti.strategy)
        # Each strategy surfaces a distinct top chunk + a shared one ("common").
        top = f"{ti.strategy}-top"
        chunks = [
            RetrievedChunk(chunk_id=top, document_id="d", score=0.9),
            RetrievedChunk(chunk_id="common", document_id="d", score=0.5),
        ]
        out = RetrieverSearchOutput(chunks=chunks)
        return _ok(name, out.model_dump(mode="json"),
                   input_hash=f"hash-{ti.strategy}", ctx=context)


class _PartialFailExecutor:
    """bm25 succeeds, vector raises RequiredToolFailed; reranker echoes order."""

    async def invoke(self, name, tool_input, context) -> ToolResult:
        if name == "retriever.rerank":
            ti = RerankInput.model_validate(tool_input)
            out = RerankOutput(chunks=list(ti.candidates),
                               scores={c.chunk_id: 1.0 for c in ti.candidates})
            return _ok(name, out.model_dump(mode="json"), ctx=context)
        ti = RetrieverSearchInput.model_validate(tool_input)
        if ti.strategy == "vector":
            raise RequiredToolFailed(name, ToolErrorCode.INTERNAL_ERROR, "boom")
        out = RetrieverSearchOutput(
            chunks=[RetrievedChunk(chunk_id="bm25-top", document_id="d", score=0.8)]
        )
        return _ok(name, out.model_dump(mode="json"), ctx=context)


class _AllFailExecutor:
    async def invoke(self, name, tool_input, context) -> ToolResult:
        raise RequiredToolFailed(name, ToolErrorCode.INTERNAL_ERROR, "down")


@pytest.mark.asyncio
async def test_dispatch_fans_out_each_strategy_and_reranks():
    ex = _StrategyVaryingExecutor()
    disp = RetrievalDispatcher(ex)
    res = await disp.execute(
        _plan("hybrid", "vector"),
        query_text="q", fetch_k=10, scenario_object="O1", scenario_depth="D1",
        entities={}, ctx=_ctx(),
    )
    # Each strategy was called exactly once with its own strategy arg.
    assert sorted(ex.calls) == ["hybrid", "vector"]
    assert ex.rerank_calls == 1
    ids = {c.chunk_id for c in res.ranked_chunks}
    # Distinct per-strategy tops + the shared chunk, deduped → all present once.
    assert {"hybrid-top", "vector-top", "common"} == ids
    assert len(res.ranked_chunks) == 3  # 'common' deduped across the two legs.
    # Reranker order is authoritative (chunk_id DESC), not search/union order.
    assert [c.chunk_id for c in res.ranked_chunks] == sorted(ids, reverse=True)
    assert res.rerank_scores  # populated by the reranker leg
    assert [s.name for s in res.executed] == ["hybrid", "vector"]
    assert res.executed[0].args_hash == "hash-hybrid"
    # 2 search legs + 1 rerank leg all recorded.
    assert len(res.tool_results) == 3


@pytest.mark.asyncio
async def test_dispatch_tolerates_partial_failure():
    disp = RetrievalDispatcher(_PartialFailExecutor())
    res = await disp.execute(
        _plan("bm25", "vector"),
        query_text="q", fetch_k=10, scenario_object=None, scenario_depth=None,
        entities={}, ctx=_ctx(),
    )
    assert [c.chunk_id for c in res.ranked_chunks] == ["bm25-top"]
    assert res.failed_strategies == ["vector"]
    assert [s.name for s in res.executed] == ["bm25"]


@pytest.mark.asyncio
async def test_dispatch_degrades_to_union_order_when_reranker_missing():
    """retriever.rerank 미배선(ToolUnknown 류) → 1차 검색(union) 순서로 graceful
    degrade, rerank_scores 빈 dict."""

    class _NoRerankExecutor:
        async def invoke(self, name, tool_input, context) -> ToolResult:
            if name == "retriever.rerank":
                raise KeyError("retriever.rerank not wired")
            ti = RetrieverSearchInput.model_validate(tool_input)
            out = RetrieverSearchOutput(chunks=[
                RetrievedChunk(chunk_id="a", document_id="d", score=0.9),
                RetrievedChunk(chunk_id="b", document_id="d", score=0.5),
            ])
            return _ok(name, out.model_dump(mode="json"), ctx=context)

    disp = RetrievalDispatcher(_NoRerankExecutor())
    res = await disp.execute(
        _plan("hybrid"), query_text="q", fetch_k=10, scenario_object=None,
        scenario_depth=None, entities={}, ctx=_ctx(),
    )
    assert [c.chunk_id for c in res.ranked_chunks] == ["a", "b"]  # union order kept
    assert res.rerank_scores == {}


class _InputCapturingExecutor:
    """검색 입력을 전부 기록 — scope(target/filters/min_token_count) passthrough 검증."""

    def __init__(self) -> None:
        self.inputs: list[RetrieverSearchInput] = []

    async def invoke(self, name, tool_input, context) -> ToolResult:
        if name == "retriever.rerank":
            ti = RerankInput.model_validate(tool_input)
            out = RerankOutput(chunks=list(ti.candidates),
                               scores={c.chunk_id: 1.0 for c in ti.candidates})
            return _ok(name, out.model_dump(mode="json"), ctx=context)
        ti = RetrieverSearchInput.model_validate(tool_input)
        self.inputs.append(ti)
        out = RetrieverSearchOutput(
            chunks=[RetrievedChunk(chunk_id=f"{ti.strategy}-c", document_id="d", score=0.7)]
        )
        return _ok(name, out.model_dump(mode="json"), ctx=context)


@pytest.mark.asyncio
async def test_dispatch_threads_scope_into_every_leg():
    ex = _InputCapturingExecutor()
    disp = RetrievalDispatcher(ex)
    await disp.execute(
        _plan("hybrid", "vector"),
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
    disp = RetrievalDispatcher(ex)
    await disp.execute(
        _plan("hybrid"),
        query_text="q", fetch_k=5, scenario_object=None, scenario_depth=None,
        entities={}, ctx=_ctx(),
    )
    ti = ex.inputs[0]
    assert ti.target == {} and ti.filters == {} and ti.min_token_count == 0


@pytest.mark.asyncio
async def test_dispatch_all_fail_reraises():
    disp = RetrievalDispatcher(_AllFailExecutor())
    with pytest.raises(RequiredToolFailed):
        await disp.execute(
            _plan("hybrid", "vector"),
            query_text="q", fetch_k=10, scenario_object=None, scenario_depth=None,
            entities={}, ctx=_ctx(),
        )
