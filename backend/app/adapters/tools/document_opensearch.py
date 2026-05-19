from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class DocumentResolveInput(BaseModel):
    citation_ids: list[str]
    chunk_ids: list[str]


class OpenSearchDocumentResolverTool:
    """Resolves citation_id / chunk_id pairs against the OpenSearch SMR index.

    A citation is resolvable when its chunk_id exists in the index. The chunk's
    document_id / page / section are returned so the verification node can
    validate completeness without re-running retrieval.
    """

    name = "document.resolve_citation"
    version = "v1-opensearch"

    def __init__(
        self,
        *,
        endpoint: str,
        index: str,
        username: str | None = None,
        password: str | None = None,
        timeout_s: float = 5.0,
        verify_certs: bool = False,
    ) -> None:
        if not endpoint:
            raise ValueError("OpenSearchDocumentResolverTool requires endpoint")
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._auth = (username, password) if username else None
        self._timeout_s = timeout_s
        self._verify = verify_certs

    async def invoke(
        self,
        tool_input: DocumentResolveInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = DocumentResolveInput.model_validate(tool_input)

        chunk_ids = list(tool_input.chunk_ids)
        citation_ids = list(tool_input.citation_ids)
        unique_chunks = sorted({c for c in chunk_ids if c})

        sources: dict[str, dict[str, Any]] = {}
        if unique_chunks:
            url = f"{self._endpoint}/{self._index}/_search"
            body = {
                "size": len(unique_chunks),
                "query": {"terms": {"chunk_id": unique_chunks}},
                "_source": ["chunk_id", "document_id", "page", "section"],
            }
            async with httpx.AsyncClient(
                timeout=self._timeout_s, verify=self._verify, auth=self._auth
            ) as client:
                resp = await client.post(
                    url, json=body, headers={"Content-Type": "application/json"}
                )
            resp.raise_for_status()
            hits = (resp.json().get("hits") or {}).get("hits") or []
            for hit in hits:
                src = hit.get("_source") or {}
                key = src.get("chunk_id") or hit.get("_id")
                if key:
                    sources[key] = src

        resolved = []
        for i, (cid, chunk_id) in enumerate(zip(citation_ids, chunk_ids, strict=False)):
            src = sources.get(chunk_id)
            resolved.append(
                {
                    "citation_id": cid,
                    "chunk_id": chunk_id,
                    "document_id": (src or {}).get("document_id"),
                    "page": (src or {}).get("page"),
                    "section": (src or {}).get("section"),
                    "resolvable": src is not None,
                    "_position": i,
                }
            )

        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"resolved": resolved},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
