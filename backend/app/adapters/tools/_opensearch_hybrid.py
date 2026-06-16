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
    scope_boost: float = 4.0,
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
    # v3.1 범위 boost-scope(target) — in-scope 문서에 가산하되 배제하지 않는다.
    # hybrid.queries 에 4번째 sub-query 를 더하면 search_pipeline 의 3-weight
    # ([.2,.3,.5]) 와 desync 되므로, boost 는 BM25 should 안에만 둔다(min_max
    # 정규화에서 BM25 컴포넌트 내 reorder 로 반영, kNN/sparse 가 out-of-scope 도
    # 독립 표면화 → recall-safe).
    for field_name, values in (ti.target or {}).items():
        vals = [v for v in (values or []) if v]
        if vals:
            bm25_should.append(
                {"terms": {field_name: vals, "boost": scope_boost}}
            )
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
    # v3.1 hard-scope(filters) — corpus_map 이 high-confidence 일 때만 채운다.
    # 단일 값은 term, 리스트는 terms. 모든 sub-query 에 공통 적용(hybrid.filter).
    #
    # wildcard 지원(spec_driven FSAR canonical) — 값에 `*`/`?` 가 들어있으면 exact
    # term 이 아니라 keyword `wildcard` 절로 변환한다. FSAR canonical 은 같은 챕터가
    # `FSAR-Part02-Ch09` 와 `FSAR-Part02-T2-Ch09` 두 표기로 갈리고 Section 유무도
    # 다양해(`...-Ch09` vs `...-Sec9.01`), 챕터 단위 스코프는 exact 로 못 잡는다.
    # `FSAR-Part02*Ch09` 같은 패턴이 두 표기·하위섹션을 한 번에 흡수한다(인덱스 실측).
    # 리스트 내 wildcard 값은 should(OR)로 묶어 terms 와 동치 의미를 유지한다.
    # keyword 필드라 wildcard 가 동작(analyzed text 면 토큰 단위라 부정확).
    def _has_glob(v: Any) -> bool:
        return isinstance(v, str) and ("*" in v or "?" in v)

    for field_name, value in (ti.filters or {}).items():
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, (list, tuple, set)):
            vals = [v for v in value if v not in (None, "")]
            if not vals:
                continue
            globs = [v for v in vals if _has_glob(v)]
            exacts = [v for v in vals if not _has_glob(v)]
            if globs and not exacts:
                # 전부 wildcard — 단일이면 wildcard 절, 다중이면 should(OR).
                if len(globs) == 1:
                    filter_clauses.append({"wildcard": {field_name: globs[0]}})
                else:
                    filter_clauses.append({"bool": {"should": [
                        {"wildcard": {field_name: g}} for g in globs
                    ], "minimum_should_match": 1}})
            elif globs:
                # exact + wildcard 혼합 — terms(OR) ∪ wildcard(OR) 를 should 로 합친다.
                shoulds: list[dict[str, Any]] = [{"terms": {field_name: exacts}}]
                shoulds += [{"wildcard": {field_name: g}} for g in globs]
                filter_clauses.append({"bool": {"should": shoulds,
                                                "minimum_should_match": 1}})
            else:
                filter_clauses.append({"terms": {field_name: exacts}})
        elif _has_glob(value):
            filter_clauses.append({"wildcard": {field_name: value}})
        else:
            filter_clauses.append({"term": {field_name: value}})
    # v3.1 노이즈 floor(Layer 2) — 본문 토큰 < N 인 chunk(목차·헤더·단어 fragment)
    # 를 검색 모집단에서 제외. range 는 hybrid.filter 와 합성 가능(function_score
    # 미사용 — hybrid nesting 제약 회피).
    if ti.min_token_count and ti.min_token_count > 0:
        filter_clauses.append(
            {"range": {"token_count": {"gte": int(ti.min_token_count)}}}
        )

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
