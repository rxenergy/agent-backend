from __future__ import annotations

import hashlib
from typing import Any

from app.domain.retrieval import (
    RetrievedChunk,
    RetrieverSearchInput,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


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
            RetrievedChunk(
                chunk_id=f"chunk-{seed[:8]}-{i}",
                document_id=f"doc-{seed[:6]}",
                score=round(0.9 - i * 0.1, 3),
                page=10 + i,
                section=f"§{i + 1}",
                snippet=(
                    f"[fake snippet {i} for "
                    f"{tool_input.scenario_object or '?'}/{tool_input.scenario_depth or '?'}]"
                ),
            )
            for i in range(max(1, tool_input.top_k))
        ]
        output = RetrieverSearchOutput(chunks=chunks)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",  # filled by executor
            trace_id=context.trace_id,
        )
