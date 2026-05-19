from __future__ import annotations

from typing import Protocol

from app.domain.memory import SessionMemory


class SessionMemoryStore(Protocol):
    async def get(self, session_id: str) -> SessionMemory | None: ...

    async def upsert(self, memory: SessionMemory) -> None: ...

    async def expire_stale(self) -> int: ...
