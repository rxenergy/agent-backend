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
from opentelemetry.trace import Status, StatusCode

from app.domain.interaction import ChatTurn
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.llm import GrammarSpec, LLMPort, LLMUnavailableError

# 분류기 LLM 호출 전용 tracer — Phoenix 가 raw 프롬프트/응답·outcome·upstream
# 오류·축별 confidence 를 단독 분석할 수 있게 자식 LLM span 을 연다(answer_spec/
# information_need instantiator 와 동일 idiom).
_TRACER = get_tracer("classification")

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
        schema: dict[str, Any] | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt_body if prompt_body is not None else _FALLBACK_PROMPT
        self._model_options = dict(model_options or _DEFAULT_MODEL_OPTIONS)
        # guided decoding 스키마(json_schema grammar). 주입되면 백엔드가 object/depth/
        # intent/scope_tier 를 enum 에, 6 개 필드를 완결 JSON 에 강제한다(vLLM guided_json
        # / OpenAI response_format). 미주입(테스트/fake)이면 자유 생성 + 사후 파싱 폴백.
        self._schema = dict(schema) if schema else None
        # 정적 정책 핀(프롬프트 sha) — refusal 이벤트가 읽는다. registry source 가
        # 동일 sha 를 미리 계산해 넘기면 재계산을 생략(동일값 보장).
        self.policy_hash = policy_hash or _policy_hash(self._prompt)

    async def classify(
        self,
        query_text: str,
        chat_history: Iterable[ChatTurn] = (),
    ) -> ClassificationResult:
        prompt = self._prompt.replace("{query}", query_text)
        grammar = (
            GrammarSpec(kind="json_schema", value=self._schema)
            if self._schema else None
        )
        # 자식 LLM span — Phoenix 가 이 한 span 만으로 confidence=0 의 원인을 단독
        # 귀인한다: input.value=프롬프트, output.value=gemma raw 응답, classifier.outcome
        # =ok|parse_failed|unavailable, unavailable 이면 record_exception + upstream_error
        # (guided_json 거부면 400 본문이 실린다), ok 인데 0 이면 *_downgraded / *_confidence
        # 가 "어느 축이 왜 0 인가"(비정규 토큰 강등 vs 모델이 낮게 줌)를 드러낸다.
        with _TRACER.start_as_current_span("classification.llm_classify") as span:
            oi.set_kind(span, oi.KIND_LLM)
            oi.set_io(span, input_value=prompt)
            span.set_attribute("classifier.grammar_applied", grammar is not None)
            span.set_attribute("classifier.policy_hash", self.policy_hash)
            try:
                result = await self._llm.generate(
                    prompt, model_options=self._model_options, grammar=grammar
                )
            except LLMUnavailableError as exc:
                span.set_status(Status(StatusCode.ERROR, "llm_classifier_unavailable"))
                span.record_exception(exc)
                span.set_attribute("classifier.outcome", "unavailable")
                span.set_attribute("classifier.upstream_error", str(exc)[:500])
                return self._fallback(query_text, "llm_classifier_unavailable")

            oi.set_llm(
                span, model_name=result.model_id, prompt=prompt,
                completion=result.text,
                prompt_tokens=int(result.token_usage.get("prompt_tokens", 0)),
                completion_tokens=int(result.token_usage.get("completion_tokens", 0)),
            )
            parsed = _parse_json(result.text)
            if parsed is None:
                # gemma 가 JSON 이 아닌 텍스트를 냄 — 위 output.value 에 raw 가 그대로
                # 보여 무엇을 냈는지 Phoenix 에서 확인 가능(guided 미적용/모델 일탈).
                span.set_status(Status(StatusCode.ERROR, "llm_classifier_parse_failed"))
                span.set_attribute("classifier.outcome", "parse_failed")
                return self._fallback(query_text, "llm_classifier_parse_failed")

            obj = str(parsed.get("object", DEFAULT_OBJECT))
            dep = str(parsed.get("depth", DEFAULT_DEPTH))
            intent = str(parsed.get("intent", DEFAULT_INTENT))
            scope_tier = str(parsed.get("scope_tier", DEFAULT_SCOPE_TIER))
            oc = float(parsed.get("object_confidence", 0.0) or 0.0)
            dc = float(parsed.get("depth_confidence", 0.0) or 0.0)
            # 위반값은 각 축별 DEFAULT 로 강등(인라인 시절 object/depth 패턴 확장).
            object_downgraded = obj not in ("O1", "O2", "O3", "O4")
            if object_downgraded:
                obj = DEFAULT_OBJECT
                oc = 0.0
            depth_downgraded = dep not in ("D1", "D2", "D3")
            if depth_downgraded:
                dep = DEFAULT_DEPTH
                dc = 0.0
            intent_downgraded = intent not in VALID_INTENTS
            if intent_downgraded:
                intent = DEFAULT_INTENT
            scope_downgraded = scope_tier not in VALID_SCOPE_TIERS
            if scope_downgraded:
                scope_tier = DEFAULT_SCOPE_TIER
            confidence = round(min(oc, dc), 3)

            span.set_attribute("classifier.outcome", "ok")
            span.set_attribute("classifier.object", obj)
            span.set_attribute("classifier.depth", dep)
            span.set_attribute("classifier.intent", intent)
            span.set_attribute("classifier.scope_tier", scope_tier)
            span.set_attribute("classifier.object_confidence", round(oc, 3))
            span.set_attribute("classifier.depth_confidence", round(dc, 3))
            span.set_attribute("classifier.confidence", confidence)
            span.set_attribute("classifier.object_downgraded", object_downgraded)
            span.set_attribute("classifier.depth_downgraded", depth_downgraded)
            span.set_attribute("classifier.intent_downgraded", intent_downgraded)
            span.set_attribute("classifier.scope_downgraded", scope_downgraded)
            return ClassificationResult(
                scenario_object=obj,
                scenario_depth=dep,
                entities=_extract_entities(query_text),
                confidence=confidence,
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
