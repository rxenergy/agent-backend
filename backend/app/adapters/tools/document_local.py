from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.domain.retrieval import DocumentFetchSectionInput
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


class LocalDocumentFetchSectionTool:
    """v3.1 P1 — local fake. Section auto-merge·다홉은 인덱스 메타 표적 조회라
    fake 코퍼스엔 형제가 없다. 빈 결과를 돌려 워크플로가 graceful no-op 되게 한다
    (Section 확장·hop 없이 정상 응답 — make smoke·로컬 dev 무영향)."""

    name = "document.fetch_section"
    version = "v1"

    async def invoke(
        self,
        tool_input: DocumentFetchSectionInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = DocumentFetchSectionInput.model_validate(tool_input)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"chunks": []},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
