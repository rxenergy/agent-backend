from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VectorHit:
    memory_id: str
    score: float


class VectorStore(Protocol):
    """Phase 5 placeholder — approved memory ANN search."""

    async def upsert(self, memory_id: str, embedding: list[float]) -> None: ...

    async def search(self, embedding: list[float], top_k: int) -> list[VectorHit]: ...
