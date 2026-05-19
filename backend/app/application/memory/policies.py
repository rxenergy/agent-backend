from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionInjectionDecision:
    inject: bool
    reason: str


def decide_session_injection(
    *,
    has_chat_history: bool,
    prior_scenario_object: str | None,
    prior_scenario_depth: str | None,
    current_scenario_object: str,
    current_scenario_depth: str,
    prior_entities: dict[str, list[str]],
    current_entities: dict[str, list[str]],
) -> SessionInjectionDecision:
    """v2 §12.5 — gate session memory injection.

    Rules:
    - First turn (no chat history) → do not inject.
    - Scenario object/depth shifted → suppress prior memory.
    - Active-entity overlap below 50% → suppress prior memory.
    """
    if not has_chat_history:
        return SessionInjectionDecision(False, "no_chat_history")

    if prior_scenario_object and prior_scenario_object != current_scenario_object:
        return SessionInjectionDecision(False, "scenario_object_shift")
    if prior_scenario_depth and prior_scenario_depth != current_scenario_depth:
        return SessionInjectionDecision(False, "scenario_depth_shift")

    prior_flat = _flatten(prior_entities)
    current_flat = _flatten(current_entities)
    if prior_flat and current_flat:
        overlap = len(prior_flat & current_flat) / max(1, len(prior_flat))
        if overlap < 0.5:
            return SessionInjectionDecision(False, "entity_overlap_below_threshold")

    return SessionInjectionDecision(True, "follow_up")


def _flatten(entities: dict[str, list[str]]) -> set[str]:
    out: set[str] = set()
    for vals in entities.values():
        for v in vals:
            out.add(v)
    return out
