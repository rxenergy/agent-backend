from __future__ import annotations

from typing import Protocol


class DenseEncoderPort(Protocol):
    """Dense embedding encoder for hybrid retrieval (e.g., multilingual-e5-large)."""

    dim: int

    def encode_query(self, text: str) -> list[float]: ...

    def warmup(self) -> None: ...


class SparseEncoderPort(Protocol):
    """SPLADE-style sparse encoder producing token→weight maps for OpenSearch rank_features."""

    def encode_query(self, text: str) -> dict[str, float]: ...

    # v3.1 Node 5 sparse reranker 용 — 후보 본문을 배치로 인코딩(검색 시점엔 인덱스에
    # 미리 인코딩돼 있으나, rerank 는 후보 본문을 on-the-fly 로 희소 벡터화해야 한다).
    def encode_documents(self, texts: list[str]) -> list[dict[str, float]]: ...

    def warmup(self) -> None: ...
