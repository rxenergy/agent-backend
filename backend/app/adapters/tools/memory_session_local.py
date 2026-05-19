from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from app.domain.memory import SessionMemory
from app.domain.tools import ToolResult
from app.ports.memory_store import SessionMemoryStore
from app.ports.tool import ToolExecutionContext


class SessionLoadInput(BaseModel):
    session_id: str | None


class SessionUpdateInput(BaseModel):
    session_id: str
    recent_turns: list[dict[str, str]] = []
    active_entities: dict[str, list[str]] = {}
    active_scenario_object: str | None = None
    active_scenario_depth: str | None = None
    conversation_summary: str = ""
    last_retrieved_chunk_ids: list[str] = []
    last_memory_ids_used: list[str] = []


class SessionLoadTool:
    name = "memory.session_load"
    version = "v1"

    def __init__(self, store: SessionMemoryStore) -> None:
        self._store = store

    async def invoke(
        self,
        tool_input: SessionLoadInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = SessionLoadInput.model_validate(tool_input)
        sid = tool_input.session_id
        if not sid:
            return ToolResult(
                tool_name=self.name,
                tool_version=self.version,
                status="success",
                output={"present": False},
                latency_ms=0,
                input_hash="",
                trace_id=context.trace_id,
            )
        memory = await self._store.get(sid)
        if memory is None:
            return ToolResult(
                tool_name=self.name,
                tool_version=self.version,
                status="success",
                output={"present": False},
                latency_ms=0,
                input_hash="",
                trace_id=context.trace_id,
            )
        expires_at = memory.expires_at
        if expires_at and expires_at < datetime.now(tz=timezone.utc):
            return ToolResult(
                tool_name=self.name,
                tool_version=self.version,
                status="success",
                output={"present": False, "reason": "expired"},
                latency_ms=0,
                input_hash="",
                trace_id=context.trace_id,
            )
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={
                "present": True,
                "active_entities": memory.active_entities,
                "active_scenario_object": memory.active_scenario_object,
                "active_scenario_depth": memory.active_scenario_depth,
                "conversation_summary": memory.conversation_summary,
                "recent_turns": [
                    {"role": t.role, "content": t.content} for t in memory.recent_turns
                ],
                "last_retrieved_chunk_ids": memory.last_retrieved_chunk_ids,
                "last_memory_ids_used": memory.last_memory_ids_used,
            },
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )


class SessionUpdateTool:
    name = "memory.session_update"
    version = "v1"

    def __init__(self, store: SessionMemoryStore, ttl_days: int) -> None:
        self._store = store
        self._ttl_days = ttl_days

    async def invoke(
        self,
        tool_input: SessionUpdateInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = SessionUpdateInput.model_validate(tool_input)
        from app.domain.interaction import ChatTurn

        now = datetime.now(tz=timezone.utc)
        from datetime import timedelta

        memory = SessionMemory(
            session_id=tool_input.session_id,
            user_id=context.user_id,
            project_id=context.project_id,
            active_entities=tool_input.active_entities,
            active_scenario_object=tool_input.active_scenario_object,
            active_scenario_depth=tool_input.active_scenario_depth,
            conversation_summary=tool_input.conversation_summary,
            recent_turns=[
                ChatTurn(role=t.get("role", "user"), content=t.get("content", ""))
                for t in tool_input.recent_turns
            ],
            last_retrieved_chunk_ids=tool_input.last_retrieved_chunk_ids,
            last_memory_ids_used=tool_input.last_memory_ids_used,
            updated_at=now,
            expires_at=now + timedelta(days=self._ttl_days),
        )
        await self._store.upsert(memory)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"session_id": tool_input.session_id, "ttl_days": self._ttl_days},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
