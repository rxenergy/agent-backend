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

    def warmup(self) -> None: ...
