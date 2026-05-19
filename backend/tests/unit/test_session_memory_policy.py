from __future__ import annotations

from app.application.memory.policies import decide_session_injection


def test_first_turn_does_not_inject() -> None:
    d = decide_session_injection(
        has_chat_history=False,
        prior_scenario_object="O4",
        prior_scenario_depth="D2",
        current_scenario_object="O4",
        current_scenario_depth="D2",
        prior_entities={"plant": ["APR1400"]},
        current_entities={"plant": ["APR1400"]},
    )
    assert d.inject is False
    assert d.reason == "no_chat_history"


def test_scenario_shift_suppresses() -> None:
    d = decide_session_injection(
        has_chat_history=True,
        prior_scenario_object="O4",
        prior_scenario_depth="D2",
        current_scenario_object="O2",  # shifted
        current_scenario_depth="D2",
        prior_entities={"plant": ["APR1400"]},
        current_entities={"plant": ["APR1400"]},
    )
    assert d.inject is False
    assert d.reason == "scenario_object_shift"


def test_entity_overlap_below_threshold_suppresses() -> None:
    d = decide_session_injection(
        has_chat_history=True,
        prior_scenario_object="O4",
        prior_scenario_depth="D2",
        current_scenario_object="O4",
        current_scenario_depth="D2",
        prior_entities={"plant": ["APR1400", "i-SMR"]},
        current_entities={"plant": ["NuScale"]},  # no overlap
    )
    assert d.inject is False
    assert d.reason == "entity_overlap_below_threshold"


def test_follow_up_injects() -> None:
    d = decide_session_injection(
        has_chat_history=True,
        prior_scenario_object="O4",
        prior_scenario_depth="D2",
        current_scenario_object="O4",
        current_scenario_depth="D2",
        prior_entities={"plant": ["APR1400"]},
        current_entities={"plant": ["APR1400"]},
    )
    assert d.inject is True
    assert d.reason == "follow_up"
