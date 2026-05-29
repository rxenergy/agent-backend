from __future__ import annotations

from app.application.retrieval.rrf import reciprocal_rank_fusion, rrf_scores
from app.domain.retrieval import RetrievedChunk


def _c(cid: str, score: float = 0.5) -> RetrievedChunk:
    return RetrievedChunk(chunk_id=cid, document_id="d", score=score)


def test_rrf_rewards_cross_strategy_agreement():
    # 'b' appears in both lists (ranks 2 and 1) → highest fused score.
    list_a = [_c("a"), _c("b"), _c("c")]
    list_b = [_c("b"), _c("d")]
    fused = reciprocal_rank_fusion([list_a, list_b], k=60)
    assert fused[0].chunk_id == "b"
    ids = [c.chunk_id for c in fused]
    assert set(ids) == {"a", "b", "c", "d"}  # dedup union


def test_rrf_score_formula():
    list_a = [_c("a"), _c("b")]
    list_b = [_c("b")]
    sc = rrf_scores([list_a, list_b], k=60)
    assert sc["a"] == 1.0 / (60 + 1)
    assert sc["b"] == 1.0 / (60 + 2) + 1.0 / (60 + 1)
    assert sc["b"] > sc["a"]


def test_rrf_deterministic_tie_break_by_chunk_id():
    # Two single-list items at equal rank-derived score → tie broken by id asc.
    fused = reciprocal_rank_fusion([[_c("z")], [_c("a")]], k=60)
    # both have score 1/61; tie-break ascending → 'a' first.
    assert [c.chunk_id for c in fused] == ["a", "z"]


def test_rrf_top_k_truncates():
    lst = [_c("a"), _c("b"), _c("c"), _c("d")]
    fused = reciprocal_rank_fusion([lst], k=60, top_k=2)
    assert len(fused) == 2


def test_rrf_empty_lists():
    assert reciprocal_rank_fusion([], k=60) == []
    assert reciprocal_rank_fusion([[]], k=60) == []


def test_rrf_keeps_first_seen_chunk_instance():
    # first_seen wins — metadata identical across strategies on one index.
    a1 = RetrievedChunk(chunk_id="x", document_id="d", score=0.9, page=1)
    a2 = RetrievedChunk(chunk_id="x", document_id="d", score=0.1, page=99)
    fused = reciprocal_rank_fusion([[a1], [a2]], k=60)
    assert len(fused) == 1
    assert fused[0].page == 1  # from the first list
