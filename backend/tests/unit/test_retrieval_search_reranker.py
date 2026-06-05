from __future__ import annotations

import pytest

from app.adapters.reranker.identity import IdentityReranker
from app.adapters.tools.retrieval_search import RetrievalSearchTool
from app.domain.retrieval import RetrievedChunk, RetrieverSearchOutput
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext

# F-3 — retrieval.search = 하이브리드 검색 + Reranker 어댑터 내부 정렬(RRF 대체).
# RerankerPort seam + identity 폴백(score 보존), 정렬·점수 분포 노출.

_CTX = ToolExecutionContext(
    interaction_id="i", trace_id="t", app_profile="local", agent_variant="agentic_finder_v4",
)


class _StubRetriever:
    """역순 점수 chunk 를 돌려주는 내부 retriever — reranker 가 정렬을 *바꾸는지* 본다."""

    name = "retriever.search"
    version = "v1"

    async def invoke(self, tool_input, context):
        chunks = [
            RetrievedChunk(chunk_id="c-lo", document_id="d", score=0.2, snippet="lo"),
            RetrievedChunk(chunk_id="c-hi", document_id="d", score=0.9, snippet="hi"),
            RetrievedChunk(chunk_id="c-mid", document_id="d", score=0.5, snippet="mid"),
        ]
        out = RetrieverSearchOutput(chunks=chunks)
        return ToolResult(
            tool_name=self.name, tool_version=self.version, status="success",
            output=out.model_dump(mode="json"), latency_ms=0, input_hash="", trace_id=context.trace_id,
        )


class _FailRetriever:
    name = "retriever.search"
    version = "v1"

    async def invoke(self, tool_input, context):
        return ToolResult(
            tool_name=self.name, tool_version=self.version, status="failed",
            error_code="tool_empty_result", latency_ms=0, input_hash="", trace_id=context.trace_id,
        )


@pytest.mark.asyncio
async def test_identity_reranker_sorts_by_score_descending() -> None:
    chunks = [
        RetrievedChunk(chunk_id="a", document_id="d", score=0.3),
        RetrievedChunk(chunk_id="b", document_id="d", score=0.8),
    ]
    ranked = await IdentityReranker().rerank("q", chunks)
    assert [r.chunk.chunk_id for r in ranked] == ["b", "a"]
    # score 보존 — rerank_score 가 검색 점수와 같다(identity).
    assert ranked[0].rerank_score == 0.8
    assert ranked[0].chunk.score == 0.8


@pytest.mark.asyncio
async def test_identity_reranker_top_k() -> None:
    chunks = [RetrievedChunk(chunk_id=str(i), document_id="d", score=i / 10) for i in range(5)]
    ranked = await IdentityReranker().rerank("q", chunks, top_k=2)
    assert [r.chunk.chunk_id for r in ranked] == ["4", "3"]


@pytest.mark.asyncio
async def test_retrieval_search_tool_reorders_and_exposes_scores() -> None:
    tool = RetrievalSearchTool(retriever=_StubRetriever(), reranker=IdentityReranker())
    result = await tool.invoke({"query_text": "i-SMR", "top_k": 3}, _CTX)
    assert result.tool_name == "retrieval.search"
    assert result.status == "success"
    out = RetrieverSearchOutput.model_validate(result.output)
    # 검색 역순 입력이 reranker 로 점수 내림차순 정렬된다.
    assert [c.chunk_id for c in out.chunks] == ["c-hi", "c-mid", "c-lo"]
    # rerank_scores 가 chunks 와 같은 순서로 노출(FinderRound 계측 입력).
    assert out.rerank_scores == [0.9, 0.5, 0.2]


@pytest.mark.asyncio
async def test_retrieval_search_propagates_inner_failure() -> None:
    tool = RetrievalSearchTool(retriever=_FailRetriever(), reranker=IdentityReranker())
    result = await tool.invoke({"query_text": "x", "top_k": 3}, _CTX)
    assert result.status == "failed"
    assert result.tool_name == "retrieval.search"
