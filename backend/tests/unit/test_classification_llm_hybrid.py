from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.application.classification.hybrid import HybridClassifier
from app.application.classification.llm import LLMClassifier
from app.application.classification.rule import RuleClassifier
from app.ports.llm import LLMResult

# 분류 프롬프트 본문은 registry 가 source of truth 다(ClassificationPromptSource).
# 단위 테스트는 JSON 파싱·강등 분기만 검증하므로 최소 본문을 직접 주입한다
# (프로덕션 배선은 registry 본문을 주입 — test_classification_prompt_source 가 검증).
_TEST_PROMPT = '분류: {query}'


def _llm_classifier(llm) -> LLMClassifier:
    return LLMClassifier(llm, prompt_body=_TEST_PROMPT)


@dataclass
class _StubLLM:
    text: str

    async def generate(self, prompt: str, *, model_options=None):
        return LLMResult(text=self.text, token_usage={"prompt_tokens": 1, "completion_tokens": 1}, model_id="stub")


@pytest.mark.asyncio
async def test_llm_classifier_parses_json() -> None:
    llm = _StubLLM(
        text='{"object":"O2","depth":"D3","object_confidence":0.9,"depth_confidence":0.8}'
    )
    r = await _llm_classifier(llm).classify("RG 1.157 원문 정의")
    assert r.scenario_object == "O2"
    assert r.scenario_depth == "D3"
    assert r.confidence == 0.8


@pytest.mark.asyncio
async def test_llm_classifier_handles_garbage_response() -> None:
    llm = _StubLLM(text="```json\n{\"object\":\"O1\",\"depth\":\"D2\",\"object_confidence\":0.7,\"depth_confidence\":0.6}\n```")
    r = await _llm_classifier(llm).classify("NuScale 설계")
    assert r.scenario_object == "O1"
    assert r.scenario_depth == "D2"


@pytest.mark.asyncio
async def test_llm_classifier_returns_default_on_unparsable() -> None:
    r = await _llm_classifier(_StubLLM(text="죄송합니다 답변 못합니다")).classify("NuScale")
    assert r.confidence == 0.0
    assert r.low_confidence_reason


@pytest.mark.asyncio
async def test_hybrid_prefers_rule_when_confident() -> None:
    # vendor + technical 키워드가 풍부해 rule이 충분히 confident
    rule = RuleClassifier()
    llm = _llm_classifier(_StubLLM(text='{"object":"O3","depth":"D1","object_confidence":0.95,"depth_confidence":0.95}'))
    h = HybridClassifier(rule, llm, escalate_below=0.2)
    r = await h.classify("NuScale의 PCS 설계 메커니즘과 수치는?")
    assert r.classifier_backend == "rule"
    assert r.scenario_object == "O1"


@pytest.mark.asyncio
async def test_hybrid_escalates_to_llm_when_rule_low() -> None:
    rule = RuleClassifier()
    llm = _llm_classifier(_StubLLM(text='{"object":"O2","depth":"D3","object_confidence":0.9,"depth_confidence":0.9}'))
    h = HybridClassifier(rule, llm, escalate_below=0.9)
    r = await h.classify("질문")  # rule will give 0 confidence
    assert r.classifier_backend == "hybrid"
    assert r.scenario_object == "O2"
    assert r.scenario_depth == "D3"
