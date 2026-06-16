from __future__ import annotations

from app.application.memory.policies import decide_session_injection


def test_first_turn_does_not_inject() -> None:
    d = decide_session_injection(
        has_history=False,
        variant_switched=False,
        continuity_signals={"route": ("retrieval", "retrieval")},
        prior_references=["10 CFR 50.46"],
        current_references=["10 CFR 50.46"],
    )
    assert d.inject is False
    assert d.reason == "no_history"


def test_variant_switch_suppresses() -> None:
    d = decide_session_injection(
        has_history=True,
        variant_switched=True,
        prior_references=["10 CFR 50.46"],
        current_references=["10 CFR 50.46"],
    )
    assert d.inject is False
    assert d.reason == "variant_switch"


def test_continuity_signal_shift_suppresses() -> None:
    d = decide_session_injection(
        has_history=True,
        variant_switched=False,
        continuity_signals={
            "route": ("retrieval", "retrieval"),
            "authority": ("binding", "guidance"),  # shifted
        },
        prior_references=["10 CFR 50.46"],
        current_references=["10 CFR 50.46"],
    )
    assert d.inject is False
    assert d.reason == "authority_shift"


def test_topic_shift_suppresses() -> None:
    d = decide_session_injection(
        has_history=True,
        variant_switched=False,
        current_topic_signature="seismic",
        prior_topic_signature="eccs",
        prior_references=["10 CFR 50.46"],
        current_references=["10 CFR 50.46"],
    )
    assert d.inject is False
    assert d.reason == "topic_shift"


def test_reference_overlap_below_threshold_suppresses() -> None:
    d = decide_session_injection(
        has_history=True,
        variant_switched=False,
        prior_references=["10 CFR 50.46", "RG 1.157"],
        current_references=["GDC 35"],  # no overlap
    )
    assert d.inject is False
    assert d.reason == "reference_overlap_below_threshold"


def test_follow_up_injects_with_matched_references() -> None:
    d = decide_session_injection(
        has_history=True,
        variant_switched=False,
        continuity_signals={"route": ("retrieval", "retrieval")},
        prior_references=["10 CFR 50.46", "RG 1.157"],
        current_references=["10 CFR 50.46"],
    )
    assert d.inject is True
    assert d.reason == "follow_up"
    assert d.matched_references == ["10 CFR 50.46"]


def test_pure_anaphora_injects_when_current_refs_empty() -> None:
    # 후속이 새 명시참조 없이 "그건 왜?" — current refs 비어있음 → overlap 게이트 미적용,
    # prior refs 전체를 anaphora 해소용으로 동반.
    d = decide_session_injection(
        has_history=True,
        variant_switched=False,
        continuity_signals={"route": ("retrieval", "retrieval")},
        prior_references=["10 CFR 50.46"],
        current_references=[],
    )
    assert d.inject is True
    assert d.reason == "follow_up"
    assert d.matched_references == ["10 CFR 50.46"]
