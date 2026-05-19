from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from app.domain.tools import ToolResult


@dataclass(frozen=True)
class ToolExecutionContext:
    """v2 §8.4."""

    interaction_id: str
    trace_id: str
    app_profile: str
    agent_variant: str
    session_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    scenario_object: str | None = None
    scenario_depth: str | None = None
    permissions: tuple[str, ...] = field(default_factory=tuple)


class Tool(Protocol):
    name: str
    version: str

    async def invoke(
        self,
        tool_input: BaseModel | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult: ...
