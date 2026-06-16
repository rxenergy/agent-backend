from __future__ import annotations

from datetime import datetime, timezone

from app.domain.memory import SessionState


class InMemorySessionStateStore:
    """In-memory session-state store — used by unit tests and the
    `memory_store=in_memory` profile (non-postgres boots)."""

    def __init__(self) -> None:
        self._store: dict[str, SessionState] = {}

    async def get(self, session_id: str) -> SessionState | None:
        return self._store.get(session_id)

    async def upsert(self, state: SessionState) -> None:
        self._store[state.session_id] = state

    async def expire_stale(self) -> int:
        now = datetime.now(tz=timezone.utc)
        stale = [sid for sid, s in self._store.items() if s.expires_at and s.expires_at < now]
        for sid in stale:
            self._store.pop(sid, None)
        return len(stale)
