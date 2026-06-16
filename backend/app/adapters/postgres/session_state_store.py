from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg

from app.domain.interaction import ChatTurn
from app.domain.memory import RetrievalTrace, SessionState, TrackedReference


class PostgresSessionStateStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(self, session_id: str) -> SessionState | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT session_id, user_id, project_id, last_variant_id,
                       turn_count, recent_turns, running_summary,
                       tracked_references, retrieval_history, topic_signature,
                       last_memory_ids_used, variant_state,
                       updated_at, expires_at
                FROM session_state
                WHERE session_id = $1
                """,
                session_id,
            )
        if row is None:
            return None
        return SessionState(
            session_id=row["session_id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            last_variant_id=row["last_variant_id"],
            turn_count=row["turn_count"] or 0,
            recent_turns=[
                ChatTurn(role=t["role"], content=t["content"])
                for t in _as_list(row["recent_turns"])
            ],
            running_summary=row["running_summary"] or "",
            tracked_references=[
                TrackedReference(
                    ref_id=r["ref_id"],
                    ref_type=r.get("ref_type", "reference"),
                    label=r.get("label", ""),
                    first_turn=r.get("first_turn", 0),
                    last_turn=r.get("last_turn", 0),
                    salience=r.get("salience", 1.0),
                )
                for r in _as_list(row["tracked_references"])
            ],
            retrieval_history=[
                RetrievalTrace(
                    turn_index=h.get("turn_index", 0),
                    chunk_ids=list(h.get("chunk_ids", [])),
                    source_ids=list(h.get("source_ids", [])),
                )
                for h in _as_list(row["retrieval_history"])
            ],
            topic_signature=row["topic_signature"],
            last_memory_ids_used=list(_as_list(row["last_memory_ids_used"])),
            variant_state=_as_dict(row["variant_state"]),
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    async def upsert(self, state: SessionState) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO session_state (
                    session_id, user_id, project_id, last_variant_id,
                    turn_count, recent_turns, running_summary,
                    tracked_references, retrieval_history, topic_signature,
                    last_memory_ids_used, variant_state,
                    updated_at, expires_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb,
                    $9::jsonb, $10, $11::jsonb, $12::jsonb, $13, $14
                )
                ON CONFLICT (session_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    project_id = EXCLUDED.project_id,
                    last_variant_id = EXCLUDED.last_variant_id,
                    turn_count = EXCLUDED.turn_count,
                    recent_turns = EXCLUDED.recent_turns,
                    running_summary = EXCLUDED.running_summary,
                    tracked_references = EXCLUDED.tracked_references,
                    retrieval_history = EXCLUDED.retrieval_history,
                    topic_signature = EXCLUDED.topic_signature,
                    last_memory_ids_used = EXCLUDED.last_memory_ids_used,
                    variant_state = EXCLUDED.variant_state,
                    updated_at = EXCLUDED.updated_at,
                    expires_at = EXCLUDED.expires_at
                """,
                state.session_id,
                state.user_id,
                state.project_id,
                state.last_variant_id,
                state.turn_count,
                json.dumps(
                    [{"role": t.role, "content": t.content} for t in state.recent_turns]
                ),
                state.running_summary,
                json.dumps(
                    [
                        {
                            "ref_id": r.ref_id,
                            "ref_type": r.ref_type,
                            "label": r.label,
                            "first_turn": r.first_turn,
                            "last_turn": r.last_turn,
                            "salience": r.salience,
                        }
                        for r in state.tracked_references
                    ]
                ),
                json.dumps(
                    [
                        {
                            "turn_index": h.turn_index,
                            "chunk_ids": h.chunk_ids,
                            "source_ids": h.source_ids,
                        }
                        for h in state.retrieval_history
                    ]
                ),
                state.topic_signature,
                json.dumps(state.last_memory_ids_used),
                json.dumps(state.variant_state),
                state.updated_at or datetime.now(tz=timezone.utc),
                state.expires_at,
            )

    async def expire_stale(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM session_state WHERE expires_at IS NOT NULL AND expires_at < now()"
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
