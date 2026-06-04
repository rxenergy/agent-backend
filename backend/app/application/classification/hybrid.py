from __future__ import annotations

import hashlib
from typing import Iterable

from app.application.classification.llm import LLMClassifier
from app.application.classification.rule import RuleClassifier
from app.application.classification import rule as _rule_mod
from app.domain.classification import ClassificationResult
from app.domain.interaction import ChatTurn


class HybridClassifier:
    """Rule classifier 먼저, confidence가 임계값 미만이면 LLM classifier로 보강.

    LLM 결과의 confidence가 rule보다 높으면 LLM 결과 채택, 아니면 rule 유지.
    Entity 추출은 rule 결과를 신뢰한다(정규식 기반이라 deterministic).
    """

    backend = "hybrid"

    def __init__(
        self,
        rule: RuleClassifier,
        llm: LLMClassifier,
        *,
        escalate_below: float,
    ) -> None:
        self._rule = rule
        self._llm = llm
        self._escalate_below = escalate_below
        # 복합 정책 핀(원칙 5) — rule·llm 정책 해시 + escalate 임계로 hybrid 결정
        # 경로를 재현. LLM 채택 분기 결과에 싣는다(rule 채택 분기는 rule 해시 유지).
        # llm 정책 핀은 인스턴스별(프롬프트 본문 의존) — registry source 가 주입한
        # 프롬프트 sha 를 그대로 합성한다(인라인 모듈 상수 시절 대체).
        self._policy_hash = hashlib.sha256(
            f"hybrid|rule={_rule_mod._POLICY_HASH}|llm={llm.policy_hash}"
            f"|escalate_below={escalate_below}".encode("utf-8")
        ).hexdigest()[:16]
        # 정적 정책 핀(요청 불변) — refusal 이벤트가 읽음. rule 채택 분기여도
        # hybrid 구성 자체를 식별하는 복합 해시를 노출한다.
        self.policy_hash = self._policy_hash

    async def classify(
        self,
        query_text: str,
        chat_history: Iterable[ChatTurn] = (),
    ) -> ClassificationResult:
        rule_r = await self._rule.classify(query_text, chat_history)
        if rule_r.confidence >= self._escalate_below:
            return rule_r
        llm_r = await self._llm.classify(query_text, chat_history)
        if llm_r.confidence > rule_r.confidence:
            # rule이 추출한 entity는 유지(정규식이 더 신뢰됨)
            return ClassificationResult(
                scenario_object=llm_r.scenario_object,
                scenario_depth=llm_r.scenario_depth,
                entities=rule_r.entities or llm_r.entities,
                confidence=llm_r.confidence,
                object_confidence=llm_r.object_confidence,
                depth_confidence=llm_r.depth_confidence,
                low_confidence_reason=None,
                classifier_backend=self.backend,
                classifier_policy_hash=self._policy_hash,
            )
        return rule_r
