from __future__ import annotations

from typing import Any

import httpx

from app.adapters.tools._opensearch_hybrid import build_hybrid_query
from app.domain.errors import RetrievalTimeoutError, RetrievalUnavailableError
from app.domain.retrieval import (
    RetrievedChunk,
    RetrieverSearchInput,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.embedding import DenseEncoderPort, SparseEncoderPort
from app.ports.tool import ToolExecutionContext


class OpenSearchRetrieverTool:
    """Hybrid retriever (BM25 + dense kNN + sparse rank_features) against OpenSearch 3.x.

    The DSL is built by `build_hybrid_query` using injected encoders. Entity
    boosts and ``scenario_object`` filters live inside the BM25 sub-query of
    the hybrid clause; dense/sparse sub-queries stay scenario-agnostic.

    Document schema expected on the index (e.g. ``nrc-all-v3``):
        {
          "document_id":      "kins-rg-2024-001",
          "chunk_id":         "kins-rg-2024-001#p7#1",
          "title":            "...",
          "page":             7,
          "section":          "§3.2",
          "scenario_object":  "regulation",
          "doc_type":         "vendor|regulation|rai",
          "revision":         "...",
          "response_date":    "YYYY-MM-DD",
          "text":             "<chunk body>",
          "dense_e5":         [float, ...]   # knn_vector(1024)
          "sparse_fermi":     {tok: float}   # rank_features
        }
    """

    name = "retriever.search"
    version = "v2"

    def __init__(
        self,
        *,
        endpoint: str,
        index: str,
        dense_encoder: DenseEncoderPort,
        sparse_encoder: SparseEncoderPort,
        search_pipeline: str | None = None,
        dense_field: str = "dense_e5",
        sparse_field: str = "sparse_fermi",
        text_field: str = "text",
        k_dense: int = 50,
        username: str | None = None,
        password: str | None = None,
        timeout_s: float = 5.0,
        verify_certs: bool = False,
    ) -> None:
        if not endpoint:
            raise ValueError("OpenSearchRetrieverTool requires endpoint")
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._dense = dense_encoder
        self._sparse = sparse_encoder
        self._search_pipeline = search_pipeline or None
        self._dense_field = dense_field
        self._sparse_field = sparse_field
        self._text_field = text_field
        self._k_dense = k_dense
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

        hq = build_hybrid_query(
            tool_input,
            dense_encoder=self._dense,
            sparse_encoder=self._sparse,
            dense_field=self._dense_field,
            sparse_field=self._sparse_field,
            text_field=self._text_field,
            k_dense=self._k_dense,
        )
        url = f"{self._endpoint}/{self._index}/_search"
        params: dict[str, str] = {}
        if self._search_pipeline:
            params["search_pipeline"] = self._search_pipeline

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, verify=self._verify, auth=self._auth
            ) as client:
                resp = await client.post(
                    url,
                    json=hq.dsl,
                    params=params or None,
                    headers={"Content-Type": "application/json"},
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
