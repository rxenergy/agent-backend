from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from app.domain.errors import RetrievalTimeoutError, RetrievalUnavailableError
from app.domain.retrieval import (
    DocumentFetchSectionInput,
    RetrievedChunk,
    RetrieverSearchOutput,
)
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
                "_source": [
                    "chunk_id",
                    "source_id",
                    "page_start",
                    "section_path_str",
                    "section_path",
                    "doc_metadata.AccessionNumber",
                    "doc_metadata.DocumentTitle",
                    "doc_metadata.title",
                ],
            }
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout_s, verify=self._verify, auth=self._auth
                ) as client:
                    resp = await client.post(
                        url, json=body, headers={"Content-Type": "application/json"}
                    )
                if resp.status_code >= 500:
                    raise RetrievalUnavailableError(
                        f"opensearch document {resp.status_code}: {resp.text[:200]}"
                    )
                resp.raise_for_status()
                hits = (resp.json().get("hits") or {}).get("hits") or []
            except httpx.TimeoutException as exc:
                raise RetrievalTimeoutError(
                    f"opensearch document timeout: {exc}"
                ) from exc
            except httpx.HTTPStatusError as exc:
                raise RetrievalUnavailableError(
                    f"opensearch document http error: {exc}"
                ) from exc
            except httpx.RequestError as exc:
                raise RetrievalUnavailableError(
                    f"opensearch document unreachable: {exc}"
                ) from exc
            for hit in hits:
                src = hit.get("_source") or {}
                key = src.get("chunk_id") or hit.get("_id")
                if key:
                    sources[key] = src

        resolved = []
        for i, (cid, chunk_id) in enumerate(zip(citation_ids, chunk_ids, strict=False)):
            src = sources.get(chunk_id) or {}
            meta = src.get("doc_metadata") or {}
            section = src.get("section_path_str") or " > ".join(src.get("section_path") or []) or None
            document_id = src.get("source_id") or meta.get("AccessionNumber")
            resolved.append(
                {
                    "citation_id": cid,
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "page": src.get("page_start"),
                    "section": section,
                    "resolvable": bool(src),
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


def _chunk_ordinal(chunk_id: str) -> int:
    """`..._cNNNN` 의 trailing int 를 파싱(섹션 내 문단 순서). zero-pad(4자리)가
    9999 를 넘으면 사전식 정렬이 깨지므로 정수로 정렬한다. 못 찾으면 0."""
    import re

    m = re.search(r"_c(\d+)$", chunk_id or "")
    return int(m.group(1)) if m else 0


class OpenSearchDocumentFetchSectionTool:
    """v3.1 P1 — 한 Section 의 형제 문단을 메타 표적으로 일괄 fetch(relevance 없음).

    `source_id` + `section_key`(section_path 배열의 최말단 원소) 로 filter-only
    bool 쿼리. section_path 는 keyword 배열이라 term 이 그 원소를 *포함*하는 chunk
    를 매칭한다(섹션 + 하위절). source_id 필터로 문서를 한정한다(section_path 는
    문서 간 비유일). 결과는 chunk_id ordinal 순으로 정렬해 본문 순서를 복원한다.
    """

    name = "document.fetch_section"
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
            raise ValueError("OpenSearchDocumentFetchSectionTool requires endpoint")
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._auth = (username, password) if username else None
        self._timeout_s = timeout_s
        self._verify = verify_certs

    async def invoke(
        self,
        tool_input: DocumentFetchSectionInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        from app.adapters.tools.retriever_opensearch import OpenSearchRetrieverTool

        if isinstance(tool_input, dict):
            tool_input = DocumentFetchSectionInput.model_validate(tool_input)

        chunks: list[RetrievedChunk] = []
        if tool_input.source_id and tool_input.section_key:
            url = f"{self._endpoint}/{self._index}/_search"
            # P1a: full section_path 원소 exact(term). P2: 번호 prefix(keyword).
            if tool_input.match == "prefix":
                section_clause: dict[str, Any] = {
                    "prefix": {"section_path": tool_input.section_key}
                }
            else:
                section_clause = {"term": {"section_path": tool_input.section_key}}
            body = {
                "size": max(1, tool_input.max_chunks),
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"source_id": tool_input.source_id}},
                            section_clause,
                        ]
                    }
                },
                "sort": [{"chunk_id": "asc"}],
                "_source": {"excludes": ["dense_e5", "sparse_fermi"]},
            }
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout_s, verify=self._verify, auth=self._auth
                ) as client:
                    resp = await client.post(
                        url, json=body, headers={"Content-Type": "application/json"}
                    )
                if resp.status_code >= 500:
                    raise RetrievalUnavailableError(
                        f"opensearch fetch_section {resp.status_code}: {resp.text[:200]}"
                    )
                resp.raise_for_status()
                hits = (resp.json().get("hits") or {}).get("hits") or []
            except httpx.TimeoutException as exc:
                raise RetrievalTimeoutError(
                    f"opensearch fetch_section timeout: {exc}"
                ) from exc
            except httpx.HTTPStatusError as exc:
                raise RetrievalUnavailableError(
                    f"opensearch fetch_section http error: {exc}"
                ) from exc
            except httpx.RequestError as exc:
                raise RetrievalUnavailableError(
                    f"opensearch fetch_section unreachable: {exc}"
                ) from exc
            # 매핑 일관성: retriever 의 _hit_to_chunk 재사용. ordinal 로 본문순 정렬.
            chunks = [OpenSearchRetrieverTool._hit_to_chunk(h) for h in hits]
            chunks.sort(key=lambda c: _chunk_ordinal(c.chunk_id))

        output = RetrieverSearchOutput(chunks=chunks)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )
