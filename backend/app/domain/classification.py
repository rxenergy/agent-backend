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
    # 분류 정책 재현 핀(원칙 5) — 어휘·정규식·부스트(rule) / 프롬프트(llm) 등
    # 정책 본문의 sha16. event 가 "어떤 정책판이 이 분류를 만들었나"를 단독 설명.
    classifier_policy_hash: str | None = None
    # open-world 의도 facet — LLM 분류기가 채움(rule/hybrid 은 None). taxonomy
    # plan §5 의 12 intent + `unknown` 폴백. 답변 *내용*을 형성하는 직교 축이며
    # (O,D)·scope_tier 와 독립. None = 미산출(비-LLM backend).
    intent: str | None = None
    # scope tier — LLM 분류기가 채움. taxonomy plan §4 의 T1(근거도메인)/
    # T2(기초·개념)/T3(메타)/T4(deflect·거부). Node 2 라우팅이 소비. None/`unknown`
    # = 보수적 처리(Node 2 가 CLARIFICATION 또는 기존 경로로 폴백).
    scope_tier: str | None = None


DEFAULT_OBJECT = ScenarioObject.O4.value
DEFAULT_DEPTH = ScenarioDepth.D2.value
DEFAULT_INTENT = "unknown"
DEFAULT_SCOPE_TIER = "unknown"

# open-world intent 어휘(taxonomy plan §5 차용). 분류기 출력 검증·이벤트 분석 기준.
VALID_INTENTS = frozenset(
    {
        "definition", "feature", "causal", "procedural", "comparison",
        "compliance", "permissibility", "verification", "status_change",
        "advisory", "meta", "exploratory", "unknown",
    }
)
# scope tier 어휘(taxonomy plan §4 차용). T1 근거도메인 / T2 기초·개념 /
# T3 메타·역량 / T4 deflect·거부 + `unknown` 폴백.
VALID_SCOPE_TIERS = frozenset({"T1", "T2", "T3", "T4", "unknown"})
