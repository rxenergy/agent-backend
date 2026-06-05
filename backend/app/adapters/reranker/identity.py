from __future__ import annotations

from app.domain.retrieval import RetrievedChunk
from app.ports.reranker import RerankedChunk, RerankerPort


class IdentityReranker(RerankerPort):
    """dev/test 폴백 — 검색(하이브리드) 점수를 그대로 rerank_score 로 쓰고 그 내림차순
    으로 정렬한다(score 보존 passthrough). 실 cross-encoder(DGX)는 배포 시 주입되는
    별도 어댑터이며, 이 폴백은 RRF 제거 후에도 정렬 seam 이 항상 존재하게 한다
    (모델 부재가 곧 '정렬 없음'이 되지 않도록). 어댑터 교체만으로 실모델 전환."""

    version = "identity/v1"

    async def rerank(
        self, query_text: str, chunks: list[RetrievedChunk], *, top_k: int | None = None
    ) -> list[RerankedChunk]:
        ranked = [RerankedChunk(chunk=c, rerank_score=float(c.score)) for c in chunks]
        ranked.sort(key=lambda r: r.rerank_score, reverse=True)
        if top_k is not None:
            ranked = ranked[: max(0, top_k)]
        return ranked
