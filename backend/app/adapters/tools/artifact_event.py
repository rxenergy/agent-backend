from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class WriteEventInput(BaseModel):
    interaction_id: str
    event_kind: str  # "interaction" | "context_snapshot" | "prompt_render"
    payload: dict[str, Any]


class WriteEventTool:
    """Thin tool wrapper. Concrete persistence is done elsewhere via EventSinkPort;
    this tool only records that the write happened as a workflow step."""

    name = "artifact.write_event"
    version = "v1"

    async def invoke(
        self,
        tool_input: WriteEventInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = WriteEventInput.model_validate(tool_input)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"interaction_id": tool_input.interaction_id, "kind": tool_input.event_kind},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
