from __future__ import annotations

from typing import Any

import httpx

from app.domain.errors import RetrievalTimeoutError, RetrievalUnavailableError
from app.domain.retrieval import (
    RetrievedChunk,
    RetrieverSearchInput,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class OpenSearchRetrieverTool:
    """BM25-only retriever against an OpenSearch index.

    Document schema expected (also written by `scripts/seed_opensearch.py`):
        {
          "document_id": "kins-rg-2024-001",
          "chunk_id":    "kins-rg-2024-001#p7#1",
          "title":       "...",
          "page":        7,
          "section":     "§3.2",
          "scenario_object": "regulation",
          "text":        "<chunk body>"
        }
    """

    name = "retriever.search"
    version = "v1"

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
            raise ValueError("OpenSearchRetrieverTool requires endpoint")
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._auth = (username, password) if username else None
        self._timeout_s = timeout_s
        self._verify = verify_certs

    async def invoke(
        self,
        tool_input: RetrieverSearchInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)

        body = self._build_query(tool_input)
        url = f"{self._endpoint}/{self._index}/_search"

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, verify=self._verify, auth=self._auth
            ) as client:
                resp = await client.post(
                    url, json=body, headers={"Content-Type": "application/json"}
                )
            if resp.status_code >= 500:
                raise RetrievalUnavailableError(
                    f"opensearch retriever {resp.status_code}: {resp.text[:200]}"
                )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise RetrievalTimeoutError(f"opensearch retriever timeout: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise RetrievalUnavailableError(
                f"opensearch retriever http error: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise RetrievalUnavailableError(
                f"opensearch retriever unreachable: {exc}"
            ) from exc

        hits = (data.get("hits") or {}).get("hits") or []
        chunks = [self._hit_to_chunk(hit) for hit in hits[: max(1, tool_input.top_k)]]
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

    def _build_query(self, ti: RetrieverSearchInput) -> dict[str, Any]:
        must: list[dict[str, Any]] = [
            {"match": {"text": {"query": ti.query_text}}},
        ]
        # Entity 부스트: 노형명/규제ID/RAI번호가 본문에 등장하면 점수 가중.
        # 한국어 standard analyzer 기준이라 should의 match로 부스팅한다.
        for vals in (ti.entities or {}).values():
            for v in vals:
                if v:
                    must.append({"match": {"text": {"query": v, "boost": 1.5}}})
        filters: list[dict[str, Any]] = []
        if ti.scenario_object:
            filters.append({"term": {"scenario_object": ti.scenario_object}})
        return {
            "size": max(1, ti.top_k),
            "query": {"bool": {"must": must, "filter": filters}},
            "_source": [
                "document_id",
                "chunk_id",
                "title",
                "page",
                "section",
                "scenario_object",
                "doc_type",
                "revision",
                "response_date",
                "text",
            ],
        }

    @staticmethod
    def _hit_to_chunk(hit: dict[str, Any]) -> RetrievedChunk:
        src = hit.get("_source", {}) or {}
        text = src.get("text", "") or ""
        return RetrievedChunk(
            chunk_id=src.get("chunk_id") or hit.get("_id"),
            document_id=src.get("document_id"),
            score=float(hit.get("_score", 0.0)),
            page=src.get("page"),
            section=src.get("section"),
            snippet=text[:512],
            doc_type=src.get("doc_type"),
            revision=src.get("revision"),
            response_date=src.get("response_date"),
        )
