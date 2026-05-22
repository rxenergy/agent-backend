"""OpenSearch 3.x hybrid query builder (BM25 + dense kNN + sparse rank_features).

Internal helper for `retriever_opensearch.OpenSearchRetrieverTool`. Encoder
dependencies are injected via protocols so unit tests can pass deterministic
fakes without loading any ML stack.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.domain.retrieval import RetrieverSearchInput
from app.ports.embedding import DenseEncoderPort, SparseEncoderPort


@dataclass
class HybridQuery:
    dsl: dict[str, Any]
    dense_dim: int
    sparse_terms: int
    encode_ms: float


def build_hybrid_query(
    ti: RetrieverSearchInput,
    *,
    dense_encoder: DenseEncoderPort,
    sparse_encoder: SparseEncoderPort,
    dense_field: str = "dense_e5",
    sparse_field: str = "sparse_fermi",
    text_field: str = "text",
    k_dense: int = 50,
    source_includes: list[str] | None = None,
) -> HybridQuery:
    """Build an OpenSearch 3.x ``hybrid`` query DSL for a retriever input.

    BM25 sub-query carries the entity boost (``should``) and ``scenario_object``
    filter so it shapes both lexical scoring and the candidate pool. Dense and
    sparse sub-queries stay scenario-agnostic — the hybrid score fusion then
    naturally amplifies hits that match all three signals.
    """
    t0 = time.perf_counter()
    dense_vec = dense_encoder.encode_query(ti.query_text)
    sparse_terms = sparse_encoder.encode_query(ti.query_text)
    encode_ms = (time.perf_counter() - t0) * 1000.0

    # BM25 sub-query mirrors the previous BM25-only behavior (entity boost +
    # scenario filter) so existing recall characteristics are preserved.
    bm25_should: list[dict[str, Any]] = []
    for vals in (ti.entities or {}).values():
        for v in vals:
            if v:
                bm25_should.append({"match": {text_field: {"query": v, "boost": 1.5}}})
    bm25_filter: list[dict[str, Any]] = []
    if ti.scenario_object:
        bm25_filter.append({"term": {"scenario_object": ti.scenario_object}})
    bm25_query: dict[str, Any] = {
        "bool": {
            "must": [{"match": {text_field: {"query": ti.query_text}}}],
            "should": bm25_should,
            "filter": bm25_filter,
        }
    }

    # Sparse rank_features assembly.
    rank_feature_clauses = [
        {
            "rank_feature": {
                "field": f"{sparse_field}.{tok}",
                "linear": {},
                "boost": weight,
            }
        }
        for tok, weight in sparse_terms.items()
    ]
    if rank_feature_clauses:
        sparse_query: dict[str, Any] = {"bool": {"should": rank_feature_clauses}}
    else:
        sparse_query = {"match_none": {}}

    dsl: dict[str, Any] = {
        "size": max(1, ti.top_k),
        "_source": source_includes
        if source_includes is not None
        else {"excludes": [dense_field, sparse_field]},
        "query": {
            "hybrid": {
                "queries": [
                    bm25_query,
                    {"knn": {dense_field: {"vector": dense_vec, "k": k_dense}}},
                    sparse_query,
                ]
            }
        },
    }

    return HybridQuery(
        dsl=dsl,
        dense_dim=len(dense_vec),
        sparse_terms=len(sparse_terms),
        encode_ms=encode_ms,
    )
