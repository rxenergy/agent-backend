from __future__ import annotations

from pathlib import Path

import yaml

from app.application.retrieval.corpus_map import CorpusMap


def _cm(**body) -> CorpusMap:
    return CorpusMap(
        collections=body.get("collections") or {},
        topic_routing=body.get("topic_routing") or [],
        chunk_quality=body.get("chunk_quality") or {},
    )


_ROUTING = [
    {"id": "reg_id", "when": {"has_entity_kinds": ["regulation_id"]},
     "scope": {"collection": ["10CFR", "RG"]}},
    {"id": "smr_design", "when": {"scenario_object_in": ["nuscale", "design"]},
     "scope": {"collection": ["SRP", "DSRS"], "search_type": "nuscale"}},
]


# --- mode-by-confidence -----------------------------------------------------


def test_high_confidence_yields_hard_filter():
    cm = _cm(topic_routing=_ROUTING, chunk_quality={"min_token_count": 12})
    d = cm.resolve_scope(
        scenario_object="design", intents=(), entities={"reactor_type": ["NuScale"]},
        confidence=0.8, tau_high=0.6, tau_low=0.3,
    )
    assert d.mode == "filter"
    assert d.filters == {"collection": ["SRP", "DSRS"], "search_type": "nuscale"}
    assert d.target == {}
    assert d.matched_rule_id == "smr_design"
    assert d.min_token_count == 12  # noise floor always carried


def test_mid_confidence_yields_boost():
    cm = _cm(topic_routing=_ROUTING)
    d = cm.resolve_scope(
        scenario_object="design", intents=(), entities={},
        confidence=0.45, tau_high=0.6, tau_low=0.3,
    )
    assert d.mode == "boost"
    assert d.filters == {}
    # boost-scope normalised to list form for terms-boost input.
    assert d.target == {"collection": ["SRP", "DSRS"], "search_type": ["nuscale"]}


def test_low_confidence_is_off_but_floor_still_applies():
    cm = _cm(topic_routing=_ROUTING, chunk_quality={"min_token_count": 9})
    d = cm.resolve_scope(
        scenario_object="design", intents=(), entities={},
        confidence=0.1, tau_high=0.6, tau_low=0.3,
    )
    assert d.mode == "off"
    assert d.filters == {} and d.target == {}
    assert d.min_token_count == 9  # floor is quality, independent of scope gate


def test_no_matching_rule_is_off():
    cm = _cm(topic_routing=_ROUTING)
    d = cm.resolve_scope(
        scenario_object="unrelated", intents=(), entities={},
        confidence=0.9, tau_high=0.6, tau_low=0.3,
    )
    assert d.mode == "off"
    assert d.matched_rule_id is None


def test_entity_kind_rule_matches():
    cm = _cm(topic_routing=_ROUTING)
    d = cm.resolve_scope(
        scenario_object="whatever", intents=(),
        entities={"regulation_id": ["RG 1.157"]},
        confidence=0.9, tau_high=0.6, tau_low=0.3,
    )
    assert d.mode == "filter"
    assert d.matched_rule_id == "reg_id"
    assert d.filters == {"collection": ["10CFR", "RG"]}


# --- min_token_count precedence (plan decision #3) --------------------------


def test_min_token_count_prefers_map_over_settings():
    cm = _cm(topic_routing=_ROUTING, chunk_quality={"min_token_count": 15})
    d = cm.resolve_scope(
        scenario_object="x", intents=(), entities={}, confidence=0.1,
        tau_high=0.6, tau_low=0.3, settings_min_token_count=99,
    )
    assert d.min_token_count == 15  # map wins


def test_min_token_count_falls_back_to_settings_when_map_silent():
    cm = _cm(topic_routing=_ROUTING)  # no chunk_quality
    d = cm.resolve_scope(
        scenario_object="x", intents=(), entities={}, confidence=0.1,
        tau_high=0.6, tau_low=0.3, settings_min_token_count=7,
    )
    assert d.min_token_count == 7


# --- hash determinism / sensitivity ----------------------------------------


def test_corpus_map_hash_is_deterministic_and_sensitive(tmp_path: Path):
    p1 = tmp_path / "m1.yaml"
    p1.write_text(yaml.safe_dump({"topic_routing": _ROUTING, "chunk_quality": {"min_token_count": 12}}))
    a = CorpusMap.from_yaml(p1)
    b = CorpusMap.from_yaml(p1)
    assert a.corpus_map_hash == b.corpus_map_hash
    assert a.corpus_map_hash and len(a.corpus_map_hash) == 16

    p2 = tmp_path / "m2.yaml"
    p2.write_text(yaml.safe_dump({"topic_routing": _ROUTING, "chunk_quality": {"min_token_count": 13}}))
    c = CorpusMap.from_yaml(p2)
    assert c.corpus_map_hash != a.corpus_map_hash  # value change → hash change


def test_default_map_is_off_with_no_floor():
    cm = CorpusMap.default()
    d = cm.resolve_scope(
        scenario_object="design", intents=(), entities={"reactor_type": ["NuScale"]},
        confidence=0.9, tau_high=0.6, tau_low=0.3,
    )
    assert d.mode == "off"
    assert d.min_token_count == 0
    assert d.corpus_map_hash  # still hashed (empty body)


def test_seed_corpus_map_yaml_loads_and_resolves():
    """배포된 tools/corpus_map.yaml 이 로드되고 규제 ID 질의를 좁힌다."""
    path = Path(__file__).resolve().parents[3] / "tools" / "corpus_map.yaml"
    cm = CorpusMap.from_yaml(path)
    # 노이즈 floor 는 기본 비활성(opt-in) — local/smoke 회귀 방지. 검증 후 운영자가 올린다.
    assert cm.min_token_count == 0
    d = cm.resolve_scope(
        scenario_object="O2", intents=(),
        entities={"regulation_id": ["10 CFR 50.46"]},
        confidence=0.9, tau_high=0.6, tau_low=0.3,
    )
    assert d.mode == "filter"
    assert "collection" in d.filters
