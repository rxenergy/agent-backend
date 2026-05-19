from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.scenario import ScenarioDepth, ScenarioObject


@dataclass(frozen=True)
class ClassificationResult:
    """Node 1 output. `confidence` is the joint confidence on (object, depth).

    `low_confidence_reason` is non-empty when the runner should refuse with
    CLARIFICATION_REQUIRED; the runner reads `confidence` against the configured
    threshold to decide.
    """

    scenario_object: str
    scenario_depth: str
    entities: dict[str, list[str]] = field(default_factory=dict)
    confidence: float = 0.0
    object_confidence: float = 0.0
    depth_confidence: float = 0.0
    low_confidence_reason: str | None = None
    classifier_backend: str = "rule"


DEFAULT_OBJECT = ScenarioObject.O4.value
DEFAULT_DEPTH = ScenarioDepth.D2.value
