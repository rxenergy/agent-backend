from __future__ import annotations

from typing import Protocol

from app.domain.memory import SessionState


class SessionStateStore(Protocol):
    async def get(self, session_id: str) -> SessionState | None: ...

    async def upsert(self, state: SessionState) -> None: ...

    async def expire_stale(self) -> int: ...
