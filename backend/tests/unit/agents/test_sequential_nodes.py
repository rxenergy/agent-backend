from __future__ import annotations

import pytest

from app.application.agents.sequential.nodes.classify import classify
from app.application.agents.sequential.state import RunState
from app.domain.classification import (
    DEFAULT_DEPTH,
    DEFAULT_OBJECT,
    ClassificationResult,
)
from app.domain.interaction import AgentRequest


@pytest.mark.asyncio
async def test_classify_falls_back_to_hardcoded_when_no_classifier() -> None:
    """Node-level test for ADR-0003 extraction. Mirrors the legacy
    `_classify` behavior used by tool-routed tests that don't inject a
    classifier — confirms refactor is byte-equivalent."""
    req = AgentRequest(interaction_id="i1", query_text="APR1400 안전계통")
    result = await classify(req, classifier=None)
    assert result.scenario_object == DEFAULT_OBJECT
    assert result.scenario_depth == DEFAULT_DEPTH
    assert result.classifier_backend == "hardcoded"
    # P2: 미주입 폴백은 confident O4/D2 가 아니라 conf 0.0 + 사유로 정직 표시.
    assert result.confidence == 0.0
    assert result.low_confidence_reason == "classifier_not_injected"
    assert result.classifier_policy_hash  # 재현 핀 존재(원칙 5)


@pytest.mark.asyncio
async def test_classify_delegates_to_injected_classifier() -> None:
    captured = {}

    class _StubClassifier:
        async def classify(self, query: str, history) -> ClassificationResult:
            captured["query"] = query
            return ClassificationResult(
                scenario_object="O1",
                scenario_depth="D2",
                entities={"regulation": ["RG 1.157"]},
                confidence=0.92,
                object_confidence=0.95,
                depth_confidence=0.9,
                classifier_backend="stub",
            )

    req = AgentRequest(interaction_id="i2", query_text="RG 1.157 요건")
    result = await classify(req, classifier=_StubClassifier())
    assert captured["query"] == "RG 1.157 요건"
    assert result.scenario_object == "O1"
    assert result.classifier_backend == "stub"


def test_run_state_constructs_with_minimum_fields() -> None:
    """RunState is the data spine for ADR-0003 node graph — sanity check
    that it accepts only the fields needed at conductor entry."""
    req = AgentRequest(interaction_id="i3", query_text="hi")
    state = RunState(request=req, started=0.0)
    assert state.classification is None
    assert state.tool_calls == []
    assert state.citations == ()
