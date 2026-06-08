from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.application.classification.hybrid import HybridClassifier
from app.application.classification.llm import LLMClassifier
from app.application.classification.rule import RuleClassifier
from app.ports.llm import LLMResult, LLMUnavailableError

# 분류 프롬프트 본문은 registry 가 source of truth 다(ClassificationPromptSource).
# 단위 테스트는 JSON 파싱·강등 분기만 검증하므로 최소 본문을 직접 주입한다
# (프로덕션 배선은 registry 본문을 주입 — test_classification_prompt_source 가 검증).
_TEST_PROMPT = '분류: {query}'


def _llm_classifier(llm) -> LLMClassifier:
    return LLMClassifier(llm, prompt_body=_TEST_PROMPT)


@dataclass
class _StubLLM:
    text: str

    async def generate(self, prompt: str, *, model_options=None, grammar=None):
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


# ----------------------------------------------------------------------
# Phoenix 관측 — 분류기 LLM 호출이 `classification.llm_classify` 자식 span 으로
# raw 응답·outcome·강등 여부를 노출해 confidence=0 을 Phoenix 단독으로 귀인할 수
# 있는지 검증한다(회귀 잠금).
# ----------------------------------------------------------------------
@dataclass
class _RaisingLLM:
    async def generate(self, prompt: str, *, model_options=None, grammar=None):
        raise LLMUnavailableError("upstream 400: guided_json rejected")


@pytest.fixture(scope="module")
def span_exporter():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # 협조적 설치 — 전역 provider 가 이미 실 SDK provider 면 거기에 exporter 만
    # 덧붙인다(OTel 은 set_tracer_provider 를 최초 1회만 적용하므로, 새로 설치하면
    # 다른 테스트 모듈의 exporter 를 굶긴다). 없으면 우리가 설치한다.
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def _classify_span(exporter):
    for sp in exporter.get_finished_spans():
        if sp.name == "classification.llm_classify":
            return sp
    return None


@pytest.mark.asyncio
async def test_span_ok_carries_per_axis_and_outcome(span_exporter) -> None:
    span_exporter.clear()
    llm = _StubLLM(
        text='{"object":"O1","depth":"D2","intent":"feature",'
        '"scope_tier":"T1","object_confidence":0.8,"depth_confidence":0.7}'
    )
    await _llm_classifier(llm).classify("NuScale 설계")
    sp = _classify_span(span_exporter)
    assert sp is not None
    a = sp.attributes
    assert a.get("classifier.outcome") == "ok"
    assert a.get("classifier.object") == "O1"
    assert a.get("classifier.object_confidence") == 0.8
    assert a.get("classifier.depth_confidence") == 0.7
    assert a.get("classifier.confidence") == 0.7  # min(oc, dc)
    # raw gemma 응답이 output 타일(llm.output_messages)에 보인다.
    assert "O1" in a.get("llm.output_messages.0.message.content", "")


@pytest.mark.asyncio
async def test_span_flags_object_downgrade_as_confidence_zero_cause(span_exporter) -> None:
    span_exporter.clear()
    # 비정규 토큰("O1 Vendor") → object 축 강등 → confidence 0. Phoenix 가
    # object_downgraded=True 로 "포맷 위반 0"을 "정직한 저신뢰"와 구분한다.
    llm = _StubLLM(
        text='{"object":"O1 Vendor","depth":"D2","intent":"feature",'
        '"scope_tier":"T1","object_confidence":0.9,"depth_confidence":0.9}'
    )
    r = await _llm_classifier(llm).classify("NuScale 설계")
    sp = _classify_span(span_exporter)
    assert sp.attributes.get("classifier.outcome") == "ok"
    assert sp.attributes.get("classifier.object_downgraded") is True
    assert sp.attributes.get("classifier.confidence") == 0.0
    assert r.confidence == 0.0


@pytest.mark.asyncio
async def test_span_parse_failed_keeps_raw_response(span_exporter) -> None:
    span_exporter.clear()
    await _llm_classifier(_StubLLM(text="죄송합니다 답을 못합니다")).classify("NuScale")
    sp = _classify_span(span_exporter)
    assert sp.attributes.get("classifier.outcome") == "parse_failed"
    # raw 비-JSON 응답이 그대로 남아 Phoenix 에서 무엇을 냈는지 보인다.
    assert "죄송" in sp.attributes.get("llm.output_messages.0.message.content", "")


@pytest.mark.asyncio
async def test_span_unavailable_carries_upstream_error(span_exporter) -> None:
    span_exporter.clear()
    await _llm_classifier(_RaisingLLM()).classify("NuScale")
    sp = _classify_span(span_exporter)
    assert sp.attributes.get("classifier.outcome") == "unavailable"
    # gemma vLLM 의 4xx 본문(guided_json 거부 등)이 span 에 박혀 Phoenix 에서 귀인.
    assert "guided_json" in sp.attributes.get("classifier.upstream_error", "")
