"""resolve_query_scope — N2 QueryFormulator 와 retrieval.rescope 가 공유하는 결정형 스코프
해소 헬퍼 단위 테스트. `_parse`(QueryFormulator)에서 추출한 순수 함수가 channel→target/
filters 매핑·배타성 게이트·canonical 검증을 planning 과 동일하게 수행하는지 잠근다.
"""

from __future__ import annotations

from app.application.intake.spec_driven_query import (
    _CANONICAL_FIELD,
    _DESIGN_FIELD,
    _STATUS_FIELD,
    resolve_query_scope,
)


def test_collection_filter_vs_boost() -> None:
    t, f, _ = resolve_query_scope({"collection": "RG", "collection_mode": "filter"})
    assert f["collection"] == ["RG"] and "collection" not in t
    t, f, _ = resolve_query_scope({"collection": "RG"})  # boost 기본
    assert t["collection"] == ["RG"] and "collection" not in f


def test_status_only_on_regulatory_collection() -> None:
    # RG + current/filter → filters[status]. 비규제(nuscale_*)면 drop + audit.
    _, f, _ = resolve_query_scope(
        {"collection": "RG", "status": "current", "status_mode": "filter"})
    assert f[_STATUS_FIELD] == ["current"]
    t, f, audit = resolve_query_scope(
        {"collection": "nuscale_FSAR", "status": "current"})
    assert _STATUS_FIELD not in f and _STATUS_FIELD not in t
    assert audit.get("status_dropped") is True


def test_design_only_on_nuscale_collection() -> None:
    _, f, _ = resolve_query_scope(
        {"collection": "nuscale_FSAR", "design": "US460", "design_mode": "filter"})
    assert f[_DESIGN_FIELD] == ["US460"]
    _, _, audit = resolve_query_scope({"collection": "10CFR", "design": "US460"})
    assert audit.get("design_dropped") is True


def test_canonical_id_10cfr_part_to_volume() -> None:
    # 10CFR-Part50 → 볼륨 canonical 환산(10CFR-Part1-50).
    _, f, _ = resolve_query_scope(
        {"collection": "10CFR", "canonical_id": "10CFR-Part50",
         "canonical_id_mode": "filter"})
    assert f[_CANONICAL_FIELD] == ["10CFR-Part1-50"]


def test_invalid_canonical_id_rejected() -> None:
    _, f, audit = resolve_query_scope(
        {"collection": "RG", "canonical_id": "garbage-id"})
    assert _CANONICAL_FIELD not in f
    assert audit.get("canonical_id_rejected") is True


def test_unknown_collection_normalized_to_none() -> None:
    # enum 외 collection → 미설정(아래 게이트 입력). status 도 함께 drop.
    t, f, _ = resolve_query_scope({"collection": "BOGUS", "status": "current"})
    assert "collection" not in t and "collection" not in f
