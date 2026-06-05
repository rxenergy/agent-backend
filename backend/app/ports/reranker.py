from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.retrieval import RetrievedChunk


@dataclass(frozen=True)
class RerankedChunk:
    """Reranker 1건 — 원 chunk + cross-encoder 점수. `chunk.score` 는 검색(하이브리드)
    점수로 보존되고, `rerank_score` 가 정렬 키다(둘을 분리해 계측이 둘 다 본다)."""

    chunk: RetrievedChunk
    rerank_score: float


class RerankerPort(Protocol):
    """하이브리드 검색 결과를 query 관련성으로 재정렬하는 포트(설계 finder §2 — RRF
    대체). 어댑터는 cross-encoder(DGX self-hosted) 또는 identity(dev/test 폴백).
    포트는 SDK-free — 와이어/모델 세부는 어댑터에 가둔다(원칙 #4)."""

    async def rerank(
        self, query_text: str, chunks: list[RetrievedChunk], *, top_k: int | None = None
    ) -> list[RerankedChunk]:
        """`chunks` 를 query 관련성 내림차순으로 재정렬해 반환한다. `top_k` 가 주어지면
        상위 N 만. 입력 순서·점수는 변형하지 않고 새 정렬을 산출한다."""
        ...
