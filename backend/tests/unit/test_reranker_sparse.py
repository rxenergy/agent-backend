from __future__ import annotations

import pytest

from app.adapters.tools.reranker_sparse import SparseRerankerTool
from app.domain.retrieval import RerankInput, RerankOutput, RetrievedChunk
from app.ports.tool import ToolExecutionContext


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="i", trace_id="t", app_profile="onprem",
        agent_variant="hierarchical_corrective_v3_1",
    )


class _StubSparseEncoder:
    """SPLADE 대용 stub — 텍스트별 token→weight 맵을 고정 반환(torch 미사용).
    배치 순서 보존(encode_documents 가 입력 순서대로 벡터를 돌려줘야 함을 검증)."""

    def __init__(self, vectors: dict[str, dict[str, float]]) -> None:
        self._vectors = vectors
        self.batch_sizes: list[int] = []

    def encode_query(self, text: str) -> dict[str, float]:
        return dict(self._vectors.get(text, {}))

    def encode_documents(self, texts: list[str]) -> list[dict[str, float]]:
        self.batch_sizes.append(len(texts))
        return [dict(self._vectors.get(t, {})) for t in texts]

    def warmup(self) -> None:  # pragma: no cover
        pass


def _chunk(cid: str, snippet: str) -> RetrievedChunk:
    return RetrievedChunk(chunk_id=cid, document_id="d", score=0.5, snippet=snippet)


def _input(query: str, *chunks: RetrievedChunk, top_k: int = 20) -> dict:
    return RerankInput(
        query_text=query, candidates=list(chunks), top_k=top_k
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_orders_by_sparse_dot_product():
    # query 희소 벡터 {eccs:2.0, cooling:1.0}. 각 후보 본문 벡터의 내적이 순위를 정한다.
    enc = _StubSparseEncoder({
        "ECCS cooling": {"eccs": 2.0, "cooling": 1.0},      # query
        "doc-strong": {"eccs": 3.0, "cooling": 2.0},        # dot = 2*3 + 1*2 = 8.0
        "doc-mid": {"eccs": 1.0},                            # dot = 2*1       = 2.0
        "doc-none": {"reactor": 5.0},                        # dot = 0.0 (공유 토큰 없음)
    })
    tool = SparseRerankerTool(enc)
    res = await tool.invoke(
        _input(
            "ECCS cooling",
            _chunk("c-strong", "doc-strong"),
            _chunk("c-mid", "doc-mid"),
            _chunk("c-none", "doc-none"),
        ),
        _ctx(),
    )
    assert res.status == "success"
    out = RerankOutput.model_validate(res.output)
    assert [c.chunk_id for c in out.chunks] == ["c-strong", "c-mid", "c-none"]
    assert out.scores["c-strong"] == 8.0
    assert out.scores["c-mid"] == 2.0
    assert out.scores["c-none"] == 0.0
    # 질의 + 후보 N건을 한 배치로 인코딩(1 forward).
    assert enc.batch_sizes == [4]


@pytest.mark.asyncio
async def test_zero_score_ties_break_by_chunk_id():
    # 공유 토큰이 전무 → 전원 0.0 → chunk_id asc 결정론 정렬.
    enc = _StubSparseEncoder({"q": {"x": 1.0}})
    tool = SparseRerankerTool(enc)
    res = await tool.invoke(
        _input("q", _chunk("c-b", "bbb"), _chunk("c-a", "aaa")),
        _ctx(),
    )
    out = RerankOutput.model_validate(res.output)
    assert [c.chunk_id for c in out.chunks] == ["c-a", "c-b"]


@pytest.mark.asyncio
async def test_top_k_truncates_after_ranking():
    enc = _StubSparseEncoder({
        "q": {"a": 1.0},
        "hi": {"a": 9.0},
        "lo": {"a": 1.0},
    })
    tool = SparseRerankerTool(enc)
    res = await tool.invoke(
        _input("q", _chunk("c-lo", "lo"), _chunk("c-hi", "hi"), top_k=1),
        _ctx(),
    )
    out = RerankOutput.model_validate(res.output)
    assert [c.chunk_id for c in out.chunks] == ["c-hi"]
    assert set(out.scores) == {"c-hi"}


@pytest.mark.asyncio
async def test_empty_candidates_returns_empty():
    enc = _StubSparseEncoder({})
    tool = SparseRerankerTool(enc)
    res = await tool.invoke(_input("q"), _ctx())
    out = RerankOutput.model_validate(res.output)
    assert out.chunks == [] and out.scores == {}
    assert enc.batch_sizes == []  # 후보 없으면 인코딩도 건너뜀.
