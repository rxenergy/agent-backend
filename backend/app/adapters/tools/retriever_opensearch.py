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

    Document schema expected on the index (``nrc-all-v1``, NRC ADAMS/govinfo):
        {
          "chunk_id":         "ML15355A364_c0001",
          "source_id":        "ML15355A364",
          "collection":       "DSRS",                       # 10CFR|DSRS|FR|RG|SRP|nuscale_*
          "search_type":      "manual",                     # manual | nuscale
          "section_path":     ["...","..."],                # 계층 섹션
          "section_path_str": "... > ...",
          "page_start":       1,
          "page_end":         3,
          "text":             "<chunk body>",
          "dense_e5":         [float, ...]                  # knn_vector(1024)
          "sparse_fermi":     {tok: float}                  # rank_features
          "doc_metadata": {
            "AccessionNumber": "ML15355A364",               # ADAMS
            "DocumentTitle":   "...",
            "DocumentDate":    "YYYY-MM-DD",
            "dateIssued":      "YYYY-MM-DD",                # govinfo
            "title":           "...",                       # govinfo
            ...
          }
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
        meta = src.get("doc_metadata") or {}
        text = src.get("text", "") or ""

        # source_id는 nrc-all-v1 의 1차 문서 식별자. 없으면 ADAMS AccessionNumber,
        # 그래도 없으면 _id 사용.
        source_id = src.get("source_id") or meta.get("AccessionNumber") or hit.get("_id") or "unknown"
        chunk_id = src.get("chunk_id") or hit.get("_id") or source_id

        # 섹션은 계층 배열을 " > " 로 합쳐 단일 문자열로 (이미 색인된 section_path_str 우선).
        section_path = src.get("section_path") or []
        section = src.get("section_path_str") or (" > ".join(section_path) if section_path else None)

        # 응답일자: ADAMS DocumentDate 우선, govinfo dateIssued 차순.
        response_date = meta.get("DocumentDate") or meta.get("dateIssued")
        title = meta.get("DocumentTitle") or meta.get("title")

        return RetrievedChunk(
            chunk_id=chunk_id,
            document_id=source_id,
            score=float(hit.get("_score", 0.0)),
            page=src.get("page_start"),
            section=section,
            snippet=text[:512],
            doc_type=src.get("collection"),
            revision=None,  # NRC 스키마에 대응 필드 없음
            response_date=response_date,
            collection=src.get("collection"),
            search_type=src.get("search_type"),
            source_id=source_id if src.get("source_id") else None,
            page_end=src.get("page_end"),
            title=title,
        )
