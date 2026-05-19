from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class RetrieverSearchInput(BaseModel):
    query_text: str
    top_k: int = 3
    scenario_object: str | None = None
    scenario_depth: str | None = None
    entities: dict[str, list[str]] = {}


class LocalRetrieverTool:
    name = "retriever.search"
    version = "v1"

    async def invoke(
        self,
        tool_input: RetrieverSearchInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)

        seed = hashlib.sha256(tool_input.query_text.encode("utf-8")).hexdigest()
        chunks = [
            {
                "chunk_id": f"chunk-{seed[:8]}-{i}",
                "document_id": f"doc-{seed[:6]}",
                "score": round(0.9 - i * 0.1, 3),
                "page": 10 + i,
                "section": f"§{i + 1}",
                "snippet": (
                    f"[fake snippet {i} for "
                    f"{tool_input.scenario_object or '?'}/{tool_input.scenario_depth or '?'}]"
                ),
            }
            for i in range(max(1, tool_input.top_k))
        ]
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"chunks": chunks},
            latency_ms=0,
            input_hash="",  # filled by executor
            trace_id=context.trace_id,
        )
