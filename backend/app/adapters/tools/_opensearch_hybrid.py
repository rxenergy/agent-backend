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

    nrc-all-v1 (NRC ADAMS/govinfo) 스키마용. ``scenario_object`` 입력은
    ``search_type`` (manual|nuscale) 으로 직접 매핑된다 ("regulation"/"manual"/
    "kins" 등 KINS 도메인 값은 manual 로 흡수). top-level ``hybrid.filter`` 로
    모든 sub-query 에 공통 필터를 적용해 BM25/dense/sparse 가 동일 모집단을
    본다.
    """
    t0 = time.perf_counter()
    dense_vec = dense_encoder.encode_query(ti.query_text)
    sparse_terms = sparse_encoder.encode_query(ti.query_text)
    encode_ms = (time.perf_counter() - t0) * 1000.0

    # BM25 sub-query — 엔티티 boost 유지. 필터는 hybrid.filter 로 분리.
    bm25_should: list[dict[str, Any]] = []
    for vals in (ti.entities or {}).values():
        for v in vals:
            if v:
                bm25_should.append({"match": {text_field: {"query": v, "boost": 1.5}}})
    bm25_query: dict[str, Any] = {
        "bool": {
            "must": [{"match": {text_field: {"query": ti.query_text}}}],
            "should": bm25_should,
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
        sparse_query: dict[str, Any] = {
            "bool": {"should": rank_feature_clauses, "minimum_should_match": 1}
        }
    else:
        sparse_query = {"match_none": {}}

    # 공통 필터 — nrc-all-v1 도메인에 맞게 매핑.
    #   regulation/manual/kins 등 → search_type=manual
    #   nuscale 등             → search_type=nuscale
    # 외 값은 무시 (필터 없이 검색).
    filter_clauses: list[dict[str, Any]] = []
    if ti.scenario_object:
        st = _scenario_to_search_type(ti.scenario_object)
        if st:
            filter_clauses.append({"term": {"search_type": st}})

    hybrid_body: dict[str, Any] = {
        "queries": [
            bm25_query,
            {"knn": {dense_field: {"vector": dense_vec, "k": k_dense}}},
            sparse_query,
        ]
    }
    if filter_clauses:
        # hybrid.filter 는 단일 query 객체. 다중 절은 bool.filter 로 감싼다.
        hybrid_body["filter"] = {"bool": {"filter": filter_clauses}}

    dsl: dict[str, Any] = {
        "size": max(1, ti.top_k),
        "_source": source_includes
        if source_includes is not None
        else {"excludes": [dense_field, sparse_field]},
        "query": {"hybrid": hybrid_body},
    }

    return HybridQuery(
        dsl=dsl,
        dense_dim=len(dense_vec),
        sparse_terms=len(sparse_terms),
        encode_ms=encode_ms,
    )


def _scenario_to_search_type(scenario_object: str) -> str | None:
    """scenario_object (RetrieverSearchInput) → nrc-all-v1 search_type 매핑.

    nrc-all-v1 의 search_type 은 ``manual`` (NRC 매뉴얼 5개 컬렉션) 과
    ``nuscale`` (NuScale 13개 컬렉션) 두 값만 갖는다. 입력이 두 도메인 중
    하나로 명확히 해석되면 그 값을 반환, 아니면 None (필터 미적용).
    """
    s = (scenario_object or "").strip().lower()
    if not s:
        return None
    if s in {"manual", "regulation", "kins", "nrc"}:
        return "manual"
    if s in {"nuscale", "vendor"}:
        return "nuscale"
    return None
