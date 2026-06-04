from __future__ import annotations

import hashlib
from typing import Any

from app.domain.classification import DEFAULT_DEPTH, DEFAULT_OBJECT, ClassificationResult
from app.domain.interaction import AgentRequest

# hardcoded 폴백 정책 핀(원칙 5) — 고정 상수.
_HARDCODED_POLICY_HASH = hashlib.sha256(
    b"hardcoded:default_object=O4:default_depth=D2:confidence=0.0"
).hexdigest()[:16]


async def classify(
    request: AgentRequest, classifier: Any | None
) -> ClassificationResult:
    """Node 1 — Intent classification (sequential workflow step 1).

    `classifier=None` 은 분류기 *미주입*(테스트/오설정) 경로다. 과거엔
    confidence 0.5 로 O4/D2 를 *확신하는 것처럼* 반환해 silent 라우팅을 유발했다
    (P2). 이제 confidence 0.0 + `low_confidence_reason="classifier_not_injected"`
    로 정직하게 표시한다 — scope 는 보수적으로 off 되고 event 에 미주입 사유가
    남는다. (프로덕션은 profiles 가 항상 분류기를 주입하므로 이 경로 미발생.)
    """
    if classifier is None:
        return ClassificationResult(
            scenario_object=DEFAULT_OBJECT,
            scenario_depth=DEFAULT_DEPTH,
            entities={},
            confidence=0.0,
            object_confidence=0.0,
            depth_confidence=0.0,
            low_confidence_reason="classifier_not_injected",
            classifier_backend="hardcoded",
            classifier_policy_hash=_HARDCODED_POLICY_HASH,
        )
    return await classifier.classify(request.query_text, request.chat_history)
