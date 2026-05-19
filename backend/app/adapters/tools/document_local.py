from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class DocumentResolveInput(BaseModel):
    citation_ids: list[str]
    chunk_ids: list[str]


class LocalDocumentResolverTool:
    name = "document.resolve_citation"
    version = "v1"

    async def invoke(
        self,
        tool_input: DocumentResolveInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = DocumentResolveInput.model_validate(tool_input)

        resolved = [
            {
                "citation_id": cid,
                "chunk_id": chunk_id,
                "document_id": f"doc-from-{chunk_id[:8]}",
                "page": 10 + i,
                "resolvable": True,
            }
            for i, (cid, chunk_id) in enumerate(
                zip(tool_input.citation_ids, tool_input.chunk_ids, strict=False)
            )
        ]
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"resolved": resolved},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
