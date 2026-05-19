from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class ApprovedSearchInput(BaseModel):
    query_text: str
    scenario_object: str | None = None
    scenario_depth: str | None = None
    top_k: int = 5


class ApprovedSearchStubTool:
    """Phase 5에서 pgvector 기반으로 교체된다. 현재는 빈 결과를 반환한다."""

    name = "memory.approved_search"
    version = "v1"

    async def invoke(
        self,
        tool_input: ApprovedSearchInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"hits": []},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
