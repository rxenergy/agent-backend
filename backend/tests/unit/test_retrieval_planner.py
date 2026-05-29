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


def test_comparison_intent_adds_bm25():
    p = _planner()
    plan = p.plan(scenario_object="O1", scenario_depth="D2", entities={}, intents=("comparison",))
    assert plan.rule_id == "comparison_multi_strategy"
    assert [s.name for s in plan.strategies] == ["hybrid", "bm25"]


def test_regulation_scenario_adds_bm25():
    p = _planner()
    plan = p.plan(scenario_object="O2", scenario_depth="D2", entities={}, intents=())
    assert plan.rule_id == "regulation_clause"
    assert "bm25" in [s.name for s in plan.strategies]


def test_regulation_id_entity_triggers_rule():
    p = _planner()
    plan = p.plan(
        scenario_object="O1", scenario_depth="D2",
        entities={"regulation_id": ["RG_1_157"]}, intents=(),
    )
    assert plan.rule_id == "has_regulation_id"


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
