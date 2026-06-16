from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SessionInjectionDecision:
    inject: bool
    reason: str
    # anaphora 해소에 동반할 prior 참조(주입 시 — 호출자가 prompt 에 싣는다).
    matched_references: list[str] = field(default_factory=list)


def decide_session_injection(
    *,
    has_history: bool,
    variant_switched: bool,
    current_topic_signature: str | None = None,
    prior_topic_signature: str | None = None,
    prior_references: list[str] | None = None,
    current_references: list[str] | None = None,
    continuity_signals: dict[str, tuple] | None = None,
    overlap_threshold: float = 0.5,
) -> SessionInjectionDecision:
    """범용 세션 주입 게이트(SessionState 교체판). variant-agnostic — 연속성 신호의
    *의미*를 모르고 결정론 규칙만 적용한다(신호=모델 산출, 결정=코드; CLAUDE.md #6,
    feedback_model_over_rule). 설계: docs/plans/spec_driven_session_memory.design.v1.md §4.

    `continuity_signals` 는 variant 가 자유롭게 주입하는 (prior, current) 신호 쌍
    (spec_driven={"route":(...),"authority":(...)}, finder={"scenario_object":(...),
    "scenario_depth":(...)}). 게이트는 "양쪽 값이 있고 서로 다르면 끊는다"만 본다.

    규칙(순서대로):
      1. not has_history                  → (False, "no_history")
      2. variant_switched                 → (False, "variant_switch")
      3. continuity_signals 중 양쪽 존재·불일치 → (False, "{key}_shift")  # 입력 순서
      4. topic_signature 양쪽 존재·불일치  → (False, "topic_shift")
      5. ref overlap < threshold (양쪽 refs 비어있지 않을 때만) → (False, "reference_overlap_below_threshold")
      6. else                             → (True, "follow_up")
    """
    if not has_history:
        return SessionInjectionDecision(False, "no_history")
    if variant_switched:
        return SessionInjectionDecision(False, "variant_switch")

    for key, pair in (continuity_signals or {}).items():
        prior_v, current_v = pair
        if prior_v and current_v and prior_v != current_v:
            return SessionInjectionDecision(False, f"{key}_shift")

    if (
        prior_topic_signature
        and current_topic_signature
        and prior_topic_signature != current_topic_signature
    ):
        return SessionInjectionDecision(False, "topic_shift")

    prior_refs = prior_references or []
    current_refs = current_references or []
    prior_set = set(prior_refs)
    current_set = set(current_refs)
    matched = [r for r in prior_refs if r in current_set]
    if prior_set and current_set:
        overlap = len(prior_set & current_set) / max(1, len(prior_set))
        if overlap < overlap_threshold:
            return SessionInjectionDecision(
                False, "reference_overlap_below_threshold", matched
            )

    # matched_references — overlap 가 있으면 교집합, 한쪽이 비어 순수 anaphora 면 prior 전체.
    matched_refs = matched if matched else (prior_refs if not current_set else [])
    return SessionInjectionDecision(True, "follow_up", matched_refs)
