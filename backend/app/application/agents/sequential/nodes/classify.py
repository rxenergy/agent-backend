from __future__ import annotations

from typing import Any

from app.domain.classification import DEFAULT_DEPTH, DEFAULT_OBJECT, ClassificationResult
from app.domain.interaction import AgentRequest


async def classify(
    request: AgentRequest, classifier: Any | None
) -> ClassificationResult:
    """Node 1 — Intent classification (sequential workflow step 1).

    Extracted from `SequentialToolRoutedRunner._classify` (ADR-0003). Behavior
    is byte-identical: `classifier=None` returns the legacy hardcoded O4/D2
    fallback used by unit tests that don't inject a classifier.
    """
    if classifier is None:
        return ClassificationResult(
            scenario_object=DEFAULT_OBJECT,
            scenario_depth=DEFAULT_DEPTH,
            entities={},
            confidence=0.5,
            object_confidence=0.5,
            depth_confidence=0.5,
            classifier_backend="hardcoded",
        )
    return await classifier.classify(request.query_text, request.chat_history)
