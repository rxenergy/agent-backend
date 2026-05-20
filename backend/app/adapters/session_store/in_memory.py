from __future__ import annotations

from datetime import datetime, timezone

from app.domain.memory import SessionMemory


class InMemorySessionMemoryStore:
    """Used by unit tests and the fake_echo_v0 variant."""

    def __init__(self) -> None:
        self._store: dict[str, SessionMemory] = {}

    async def get(self, session_id: str) -> SessionMemory | None:
        return self._store.get(session_id)

    async def upsert(self, memory: SessionMemory) -> None:
        self._store[memory.session_id] = memory

    async def expire_stale(self) -> int:
        now = datetime.now(tz=timezone.utc)
        stale = [sid for sid, m in self._store.items() if m.expires_at and m.expires_at < now]
        for sid in stale:
            self._store.pop(sid, None)
        return len(stale)
