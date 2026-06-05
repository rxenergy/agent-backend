from __future__ import annotations

from pathlib import Path

import pytest

from app.application.retrieval.planner import RetrievalPlanner

_STRATEGIES_YAML = Path(__file__).resolve().parents[3] / "tools" / "retrieval_strategies.yaml"


def _planner() -> RetrievalPlanner:
    return RetrievalPlanner.from_yaml(_STRATEGIES_YAML)


def test_default_plan_is_single_hybrid():
    p = _planner()
    plan = p.plan(scenario_object="O1", scenario_depth="D1", entities={}, intents=())
    assert plan.rule_id == "default"
    assert [s.name for s in plan.strategies] == ["hybrid"]


def test_repo_yaml_routes_everything_to_single_hybrid():
    """v3.1 RRF 제거 — 별도 bm25 leg + 다전략 룰을 폐기했다. 비교·규제·규제ID
    질의도 전부 단일 hybrid 1차 검색으로 모은 뒤 Node 5 reranker 가 재정렬한다."""
    p = _planner()
    for so, sd, ents, intents in [
        ("O1", "D2", {}, ("comparison",)),
        ("O2", "D2", {}, ()),
        ("O1", "D2", {"regulation_id": ["RG_1_157"]}, ()),
    ]:
        plan = p.plan(scenario_object=so, scenario_depth=sd, entities=ents, intents=intents)
        assert plan.rule_id == "default"
        assert [s.name for s in plan.strategies] == ["hybrid"]


def test_plan_hash_deterministic_and_entity_sensitive():
    p = _planner()
    a = p.plan(scenario_object="O1", scenario_depth="D1", entities={"reactor_type": ["i-SMR"]}, intents=())
    b = p.plan(scenario_object="O1", scenario_depth="D1", entities={"reactor_type": ["i-SMR"]}, intents=())
    c = p.plan(scenario_object="O1", scenario_depth="D1", entities={"reactor_type": ["NuScale"]}, intents=())
    assert a.plan_hash == b.plan_hash      # same inputs → same hash
    assert a.plan_hash != c.plan_hash      # different entities → different hash


def test_entity_order_does_not_change_hash():
    p = _planner()
    a = p.plan(scenario_object="O1", scenario_depth="D1",
               entities={"reactor_type": ["a", "b"]}, intents=())
    b = p.plan(scenario_object="O1", scenario_depth="D1",
               entities={"reactor_type": ["b", "a"]}, intents=())
    assert a.plan_hash == b.plan_hash  # values sorted before hashing


def test_default_planner_has_no_rules():
    p = RetrievalPlanner.default()
    plan = p.plan(scenario_object="O2", scenario_depth="D2",
                  entities={"regulation_id": ["x"]}, intents=("comparison",))
    assert plan.rule_id == "default"
    assert [s.name for s in plan.strategies] == ["hybrid"]
