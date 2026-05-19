from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg

from app.domain.interaction import ChatTurn
from app.domain.memory import SessionMemory


class PostgresSessionMemoryStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(self, session_id: str) -> SessionMemory | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT session_id, user_id, project_id, active_entities,
                       active_scenario_object, active_scenario_depth,
                       conversation_summary, recent_turns,
                       last_retrieved_chunk_ids, last_memory_ids_used,
                       updated_at, expires_at
                FROM session_memory
                WHERE session_id = $1
                """,
                session_id,
            )
        if row is None:
            return None
        return SessionMemory(
            session_id=row["session_id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            active_entities=_as_dict(row["active_entities"]),
            active_scenario_object=row["active_scenario_object"],
            active_scenario_depth=row["active_scenario_depth"],
            conversation_summary=row["conversation_summary"] or "",
            recent_turns=[
                ChatTurn(role=t["role"], content=t["content"])
                for t in _as_list(row["recent_turns"])
            ],
            last_retrieved_chunk_ids=list(_as_list(row["last_retrieved_chunk_ids"])),
            last_memory_ids_used=list(_as_list(row["last_memory_ids_used"])),
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    async def upsert(self, memory: SessionMemory) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO session_memory (
                    session_id, user_id, project_id, active_entities,
                    active_scenario_object, active_scenario_depth,
                    conversation_summary, recent_turns,
                    last_retrieved_chunk_ids, last_memory_ids_used,
                    updated_at, expires_at
                ) VALUES (
                    $1, $2, $3, $4::jsonb, $5, $6, $7, $8::jsonb,
                    $9::jsonb, $10::jsonb, $11, $12
                )
                ON CONFLICT (session_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    project_id = EXCLUDED.project_id,
                    active_entities = EXCLUDED.active_entities,
                    active_scenario_object = EXCLUDED.active_scenario_object,
                    active_scenario_depth = EXCLUDED.active_scenario_depth,
                    conversation_summary = EXCLUDED.conversation_summary,
                    recent_turns = EXCLUDED.recent_turns,
                    last_retrieved_chunk_ids = EXCLUDED.last_retrieved_chunk_ids,
                    last_memory_ids_used = EXCLUDED.last_memory_ids_used,
                    updated_at = EXCLUDED.updated_at,
                    expires_at = EXCLUDED.expires_at
                """,
                memory.session_id,
                memory.user_id,
                memory.project_id,
                json.dumps(memory.active_entities),
                memory.active_scenario_object,
                memory.active_scenario_depth,
                memory.conversation_summary,
                json.dumps([{"role": t.role, "content": t.content} for t in memory.recent_turns]),
                json.dumps(memory.last_retrieved_chunk_ids),
                json.dumps(memory.last_memory_ids_used),
                memory.updated_at or datetime.now(tz=timezone.utc),
                memory.expires_at,
            )

    async def expire_stale(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM session_memory WHERE expires_at IS NOT NULL AND expires_at < now()"
            )
        # result like "DELETE 3"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return json.loads(value)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return json.loads(value)
