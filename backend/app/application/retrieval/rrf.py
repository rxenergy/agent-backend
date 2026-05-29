from __future__ import annotations

from app.domain.retrieval import RetrievedChunk

# v3.1 Node 5 — Reciprocal Rank Fusion (Cormack et al., SIGIR 2009).
# learning-free, hyperparameter 1개(k). 다전략 검색 결과를 순위 기반으로 융합.


def reciprocal_rank_fusion(
    ranked_lists: list[list[RetrievedChunk]],
    *,
    k: int = 60,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """여러 ranked chunk 리스트를 RRF 로 융합한다.

    score(d) = Σ_i 1 / (k + rank_i(d))   (rank 는 1-based)

    - chunk_id 로 dedup. 동일 chunk 가 여러 전략에서 나오면 점수가 합산되어
      상위로 올라간다 (cross-strategy agreement 가 순위에 반영됨).
    - 반환되는 `RetrievedChunk` 인스턴스는 *first-seen* (단일 인덱스에서 메타는
      전략 간 동일). `chunk.score` 는 raw retriever 점수를 그대로 둔다 — RRF
      점수는 별도 개념이며 *순서*로만 표현한다(프로즌 모델 비변형). 다운스트림은
      리스트 순서를 융합 결과로 신뢰한다.
    - 정렬: 융합 점수 desc, 동점은 chunk_id asc (결정론).
    """
    scores: dict[str, float] = {}
    first_seen: dict[str, RetrievedChunk] = {}
    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            cid = chunk.chunk_id
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(cid, chunk)
    fused = sorted(
        first_seen.values(),
        key=lambda c: (-scores[c.chunk_id], c.chunk_id),
    )
    if top_k is not None:
        fused = fused[:top_k]
    return fused


def rrf_scores(ranked_lists: list[list[RetrievedChunk]], *, k: int = 60) -> dict[str, float]:
    """진단/감사용 — chunk_id → 융합 점수. (재현성 trace 에 실릴 수 있음)"""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
    return scores
