from __future__ import annotations

from app.adapters.tools._opensearch_hybrid import build_hybrid_query
from app.domain.retrieval import RetrieverSearchInput


class _FakeDense:
    dim = 4

    def encode_query(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    def warmup(self) -> None:  # pragma: no cover
        pass


class _FakeSparse:
    def __init__(self, terms: dict[str, float] | None = None) -> None:
        self._terms = terms if terms is not None else {"loca": 1.7, "eccs": 0.9}

    def encode_query(self, text: str) -> dict[str, float]:
        return dict(self._terms)

    def warmup(self) -> None:  # pragma: no cover
        pass


def _input(**kw) -> RetrieverSearchInput:
    base = {"query_text": "LOCA 잔열 제거", "top_k": 3}
    base.update(kw)
    return RetrieverSearchInput.model_validate(base)


def test_hybrid_dsl_has_three_subqueries():
    hq = build_hybrid_query(
        _input(), dense_encoder=_FakeDense(), sparse_encoder=_FakeSparse()
    )
    subs = hq.dsl["query"]["hybrid"]["queries"]
    assert len(subs) == 3
    # bm25 sub-query
    assert "bool" in subs[0]
    assert subs[0]["bool"]["must"] == [{"match": {"text": {"query": "LOCA 잔열 제거"}}}]
    # dense kNN
    assert subs[1]["knn"]["dense_e5"]["vector"] == [0.1, 0.2, 0.3, 0.4]
    assert subs[1]["knn"]["dense_e5"]["k"] == 50
    # sparse rank_features
    rf = subs[2]["bool"]["should"]
    fields = {c["rank_feature"]["field"] for c in rf}
    assert fields == {"sparse_fermi.loca", "sparse_fermi.eccs"}


def test_hybrid_dsl_includes_scenario_filter_and_entity_boost():
    """scenario_object → search_type 매핑은 top-level hybrid.filter 로 분리되고,
    엔티티 boost 는 BM25 sub-query 의 should 절에 그대로 남는다.
    """
    hq = build_hybrid_query(
        _input(
            scenario_object="regulation",
            entities={"reactor": ["BWRX-300", ""], "rai": ["RAI-12"]},
        ),
        dense_encoder=_FakeDense(),
        sparse_encoder=_FakeSparse(),
    )
    hybrid = hq.dsl["query"]["hybrid"]
    # 공통 필터는 hybrid.filter 에 단일 query 객체로 (bool.filter 로 감싸서).
    assert hybrid["filter"] == {
        "bool": {"filter": [{"term": {"search_type": "manual"}}]}
    }
    bm25 = hybrid["queries"][0]["bool"]
    # BM25 sub-query 자체에는 더 이상 scenario filter 가 없음.
    assert bm25.get("filter", []) == []
    boosts = [s["match"]["text"]["query"] for s in bm25["should"]]
    assert boosts == ["BWRX-300", "RAI-12"]  # empty value skipped


def test_hybrid_dsl_nuscale_search_type_filter():
    """scenario_object="nuscale" → search_type=nuscale 필터."""
    hq = build_hybrid_query(
        _input(scenario_object="nuscale"),
        dense_encoder=_FakeDense(),
        sparse_encoder=_FakeSparse(),
    )
    assert hq.dsl["query"]["hybrid"]["filter"] == {
        "bool": {"filter": [{"term": {"search_type": "nuscale"}}]}
    }


def test_hybrid_dsl_no_filter_when_scenario_object_unknown():
    """알 수 없는 scenario_object 값은 필터를 생성하지 않는다."""
    hq = build_hybrid_query(
        _input(scenario_object="other-domain"),
        dense_encoder=_FakeDense(),
        sparse_encoder=_FakeSparse(),
    )
    assert "filter" not in hq.dsl["query"]["hybrid"]


def test_hybrid_dsl_range_dict_filter_becomes_range_clause():
    """filters 값이 range dict(gte/lte/relation)면 `range` 절로 변환(spec_driven 10CFR
    Part→page). term/terms/wildcard 분기와 배타 — 스칼라/리스트 값은 영향 없음."""
    hq = build_hybrid_query(
        _input(filters={
            "doc_metadata.std_canonical_id.keyword": ["10CFR-Part1-50"],
            "page_range": {"gte": 853, "lte": 1123, "relation": "intersects"},
        }),
        dense_encoder=_FakeDense(),
        sparse_encoder=_FakeSparse(),
    )
    clauses = hq.dsl["query"]["hybrid"]["filter"]["bool"]["filter"]
    assert {"terms": {"doc_metadata.std_canonical_id.keyword": ["10CFR-Part1-50"]}} in clauses
    assert {"range": {"page_range": {"gte": 853, "lte": 1123,
                                     "relation": "intersects"}}} in clauses


def test_hybrid_dsl_empty_sparse_falls_back_to_match_none():
    hq = build_hybrid_query(
        _input(),
        dense_encoder=_FakeDense(),
        sparse_encoder=_FakeSparse(terms={}),
    )
    sparse_sub = hq.dsl["query"]["hybrid"]["queries"][2]
    assert sparse_sub == {"match_none": {}}
    assert hq.sparse_terms == 0


def test_hybrid_dsl_excludes_vector_fields_from_source():
    hq = build_hybrid_query(
        _input(), dense_encoder=_FakeDense(), sparse_encoder=_FakeSparse()
    )
    assert hq.dsl["_source"] == {"excludes": ["dense_e5", "sparse_fermi"]}


def test_hybrid_dsl_stats():
    hq = build_hybrid_query(
        _input(), dense_encoder=_FakeDense(), sparse_encoder=_FakeSparse()
    )
    assert hq.dense_dim == 4
    assert hq.sparse_terms == 2
    assert hq.encode_ms >= 0.0
