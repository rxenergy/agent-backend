from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.application.prompting.information_need_source import (
    InformationNeedPromptSource,
)
from app.ports.llm import LLMResult, LLMUnavailableError

# 프롬프트는 코드 인라인이 아니라 registry 에서 로드 — source 가 sha 검증 후
# 인스턴스화기를 만든다. 실 repo prompts/ 를 읽어 로딩 경로까지 함께 검증한다.
_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


def _make(llm):
    return InformationNeedPromptSource(_PROMPTS).build_instantiator(llm)


class _NeedLLM:
    """요구 JSON 을 돌려주는 controllable stub — 모델 경로(method='llm') 검증용."""

    model_id = "need-stub"

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def generate(self, prompt, *, model_options=None, grammar=None):
        return LLMResult(
            text=json.dumps(self._payload),
            token_usage={"prompt_tokens": 1, "completion_tokens": 1},
            model_id=self.model_id,
        )

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):  # pragma: no cover
        raise NotImplementedError


class _GarbageLLM(_NeedLLM):
    """JSON 이 아닌 텍스트(예: 코드펜스 없는 산문) — fallback 트리거 검증."""

    async def generate(self, prompt, *, model_options=None, grammar=None):
        return LLMResult(text="죄송합니다, 분류할 수 없습니다.",
                         token_usage={}, model_id=self.model_id)


class _UnavailableLLM:
    model_id = "down"

    async def generate(self, prompt, *, model_options=None, grammar=None):
        raise LLMUnavailableError("vLLM down")

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_llm_path_parses_slots_subquestions_and_version() -> None:
    llm = _NeedLLM({
        "required_slots": [
            {"name": "governing_clause", "required": True},
            {"name": "condition_exception", "required": False},
        ],
        "sub_questions": ["i-SMR ECCS 요건은?", "예외 조항은?"],
        "version_constraint": "2024-06-01",
        "multi_intent": True,
    })
    res = await _make(llm).instantiate(
        "i-SMR ECCS 준수 요건과 예외는?",
        scenario_object="O1", scenario_depth="D2", intent="compliance",
    )
    assert res.method == "llm"
    assert [s.name for s in res.required_slots] == ["governing_clause", "condition_exception"]
    assert res.required_slots[1].required is False
    assert [q.text for q in res.sub_questions] == ["i-SMR ECCS 요건은?", "예외 조항은?"]
    assert res.version_constraint == "2024-06-01"
    assert res.multi_intent is True
    # 재현: prompt_hash(핀)·information_need_hash(fingerprint) 둘 다 16-hex.
    assert len(res.prompt_hash) == 16 and len(res.information_need_hash) == 16
    # policy_hash = registry 프롬프트 fragment sha(정적 정책 핀) — source 가 주입.
    assert res.policy_hash and len(res.policy_hash) == 16


@pytest.mark.asyncio
async def test_fallback_on_garbage_uses_intent_prior() -> None:
    res = await _make(_GarbageLLM({})).instantiate(
        "i-SMR 준수 요건?", scenario_object="O1", scenario_depth="D2",
        intent="compliance",
    )
    assert res.method == "fallback"
    # compliance prior = governing_clause·requirement_text·applicability·effective_version
    assert [s.name for s in res.required_slots] == [
        "governing_clause", "requirement_text", "applicability", "effective_version",
    ]
    assert res.sub_questions == ()
    assert res.version_constraint is None


@pytest.mark.asyncio
async def test_fallback_on_unavailable() -> None:
    res = await _make(_UnavailableLLM()).instantiate(
        "정의가 뭐야?", scenario_object="O4", scenario_depth="D1", intent="definition",
    )
    assert res.method == "fallback"
    assert [s.name for s in res.required_slots] == ["definition", "source_clause", "scope"]


@pytest.mark.asyncio
async def test_unknown_intent_falls_back_to_default_slots() -> None:
    res = await _make(_UnavailableLLM()).instantiate(
        "뭔가 모호한 질의", scenario_object="O4", scenario_depth="D2", intent="unknown",
    )
    assert [s.name for s in res.required_slots] == ["governing_clause", "requirement_text"]


@pytest.mark.asyncio
async def test_empty_slots_from_llm_falls_back() -> None:
    # required_slots 가 빈 배열이면 모델 산출을 신뢰하지 않고 fallback(parsed[0] 검사).
    res = await _make(
        _NeedLLM({"required_slots": []})
    ).instantiate("q", scenario_object="O1", scenario_depth="D2", intent="compliance")
    assert res.method == "fallback"


@pytest.mark.asyncio
async def test_information_need_hash_is_deterministic() -> None:
    payload = {"required_slots": [{"name": "governing_clause"}], "version_constraint": None}
    a = await _make(_NeedLLM(payload)).instantiate(
        "q", scenario_object="O1", scenario_depth="D2", intent="compliance")
    b = await _make(_NeedLLM(payload)).instantiate(
        "q", scenario_object="O1", scenario_depth="D2", intent="compliance")
    assert a.information_need_hash == b.information_need_hash
