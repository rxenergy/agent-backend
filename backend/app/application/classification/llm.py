from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from app.application.classification.rule import _extract_entities
from app.domain.classification import (
    DEFAULT_DEPTH,
    DEFAULT_INTENT,
    DEFAULT_OBJECT,
    DEFAULT_SCOPE_TIER,
    VALID_INTENTS,
    VALID_SCOPE_TIERS,
    ClassificationResult,
)
from app.domain.interaction import ChatTurn
from app.ports.llm import LLMPort, LLMUnavailableError

# 분류 프롬프트 본문은 더 이상 코드 인라인이 아니다 — prompts/registry.yaml 의
# classification_prompts 블록(ClassificationPromptSource)이 source of truth 다.
# 테스트/오설정 폴백용 최소 본문만 둔다(프로덕션은 항상 registry 본문을 주입).
_FALLBACK_PROMPT = (
    '너는 SMR 인허가 도메인 질의 분류기다. 질의를 분류해 JSON 하나로만 답한다:\n'
    '{"object":"O1|O2|O3|O4","depth":"D1|D2|D3","intent":"<intent>",'
    '"scope_tier":"T1|T2|T3|T4","object_confidence":0.0-1.0,'
    '"depth_confidence":0.0-1.0}\n질의: {query}\n'
)
_DEFAULT_MODEL_OPTIONS = {"temperature": 0.0, "max_tokens": 200}

_RE_JSON = re.compile(r"\{.*\}", re.S)


def _policy_hash(prompt_body: str) -> str:
    """분류 정책 재현 핀(원칙 5) — 프롬프트 본문의 sha16. 프롬프트가 바뀌면
    해시가 바뀐다. entity 정규식은 rule._extract_entities 재사용이라 별도 핀 불필요."""
    return hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()[:16]


class LLMClassifier:
    backend = "llm"

    def __init__(
        self,
        llm: LLMPort,
        *,
        prompt_body: str | None = None,
        model_options: dict[str, Any] | None = None,
        policy_hash: str | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt_body if prompt_body is not None else _FALLBACK_PROMPT
        self._model_options = dict(model_options or _DEFAULT_MODEL_OPTIONS)
        # 정적 정책 핀(프롬프트 sha) — refusal 이벤트가 읽는다. registry source 가
        # 동일 sha 를 미리 계산해 넘기면 재계산을 생략(동일값 보장).
        self.policy_hash = policy_hash or _policy_hash(self._prompt)

    async def classify(
        self,
        query_text: str,
        chat_history: Iterable[ChatTurn] = (),
    ) -> ClassificationResult:
        prompt = self._prompt.replace("{query}", query_text)
        try:
            result = await self._llm.generate(prompt, model_options=self._model_options)
        except LLMUnavailableError:
            return self._fallback(query_text, "llm_classifier_unavailable")
        parsed = _parse_json(result.text)
        if parsed is None:
            return self._fallback(query_text, "llm_classifier_parse_failed")

        obj = str(parsed.get("object", DEFAULT_OBJECT))
        dep = str(parsed.get("depth", DEFAULT_DEPTH))
        intent = str(parsed.get("intent", DEFAULT_INTENT))
        scope_tier = str(parsed.get("scope_tier", DEFAULT_SCOPE_TIER))
        oc = float(parsed.get("object_confidence", 0.0) or 0.0)
        dc = float(parsed.get("depth_confidence", 0.0) or 0.0)
        # 위반값은 각 축별 DEFAULT 로 강등(인라인 시절 object/depth 패턴 확장).
        if obj not in ("O1", "O2", "O3", "O4"):
            obj = DEFAULT_OBJECT
            oc = 0.0
        if dep not in ("D1", "D2", "D3"):
            dep = DEFAULT_DEPTH
            dc = 0.0
        if intent not in VALID_INTENTS:
            intent = DEFAULT_INTENT
        if scope_tier not in VALID_SCOPE_TIERS:
            scope_tier = DEFAULT_SCOPE_TIER
        return ClassificationResult(
            scenario_object=obj,
            scenario_depth=dep,
            entities=_extract_entities(query_text),
            confidence=round(min(oc, dc), 3),
            object_confidence=round(oc, 3),
            depth_confidence=round(dc, 3),
            classifier_backend=self.backend,
            classifier_policy_hash=self.policy_hash,
            intent=intent,
            scope_tier=scope_tier,
        )

    def _fallback(self, query_text: str, reason: str) -> ClassificationResult:
        return ClassificationResult(
            scenario_object=DEFAULT_OBJECT,
            scenario_depth=DEFAULT_DEPTH,
            entities=_extract_entities(query_text),
            confidence=0.0,
            low_confidence_reason=reason,
            classifier_backend=self.backend,
            classifier_policy_hash=self.policy_hash,
            intent=DEFAULT_INTENT,
            scope_tier=DEFAULT_SCOPE_TIER,
        )


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _RE_JSON.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
