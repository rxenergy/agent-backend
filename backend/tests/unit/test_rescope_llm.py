"""RescopeLlm — none_necessary 슬롯 스코프 재계획 단위 테스트(fake LLMPort, 컨테이너 불요).

검증 포인트:
  - 모델이 낸 planning 스코프 채널(collection/status/design/canonical_id + mode)을
    결정형 게이트(`resolve_query_scope`, N2 QueryFormulator 와 공유)로 target/filters 로 해소.
  - filter mode → filters, boost mode → target. 배타성(status↔규제, design↔NuScale) 강제.
  - LLM 미가용/파싱 실패 → method="fallback" + 빈 queries(러너가 재검색 skip).
"""

from __future__ import annotations

import json

import pytest

from app.adapters.rescope_llm import RescopeLlm
from app.ports.llm import LLMResult, LLMUnavailableError


class _FakeSource:
    prompt_body = "rescope {query} {spec} {slot_name} {slot_query} " \
                  "{why_not_needed} {what_is_needed} {initial_scope} {max_queries}"
    schema = {"type": "object"}
    model_options = {"temperature": 0.0}


class _ScriptedLLM:
    def __init__(self, payload: dict | None = None, *, fail: bool = False,
                 raw: str | None = None) -> None:
        self._payload = payload or {}
        self._fail = fail
        self._raw = raw
        self.calls = 0
        self.last_prompt = ""

    @property
    def model_id(self) -> str:
        return "scripted"

    async def generate(self, prompt, *, model_options=None, grammar=None):
        self.calls += 1
        self.last_prompt = prompt
        if self._fail:
            raise LLMUnavailableError("rescope node down")
        text = self._raw if self._raw is not None else json.dumps(self._payload)
        return LLMResult(text=text, token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                         model_id=self.model_id)


async def _rescope(rescoper: RescopeLlm, *, initial_scope: dict | None = None) -> dict:
    return await rescoper.rescope(
        query_text="q", answer_spec="spec", slot_name="s", slot_query="sq",
        why_not_needed="wrong family", what_is_needed="NuScale FSAR ECCS design",
        initial_scope=initial_scope or {"filters": {"collection": ["SRP"]}},
    )


@pytest.mark.asyncio
async def test_rescope_resolves_collection_filter() -> None:
    # collection RG + filter mode → filters.collection. planning 과 동일 해소.
    llm = _ScriptedLLM({"reasoning": "switch to RG", "queries": [
        {"query_text": "passive ECCS design", "collection": "RG",
         "collection_mode": "filter"},
    ]})
    out = await _rescope(RescopeLlm(llm=llm, source=_FakeSource()))

    assert llm.calls == 1
    assert out["method"] == "llm"
    assert len(out["queries"]) == 1
    q = out["queries"][0]
    assert q["query_text"] == "passive ECCS design"
    assert q["filters"]["collection"] == ["RG"]
    assert "collection" not in q["target"]


@pytest.mark.asyncio
async def test_rescope_boost_goes_to_target() -> None:
    # collection 10CFR + boost(기본) → target.collection(filters 아님).
    llm = _ScriptedLLM({"reasoning": "boost", "queries": [
        {"query_text": "acceptance criteria", "collection": "10CFR"},
    ]})
    out = await _rescope(RescopeLlm(llm=llm, source=_FakeSource()))

    q = out["queries"][0]
    assert q["target"]["collection"] == ["10CFR"]
    assert "collection" not in q["filters"]


@pytest.mark.asyncio
async def test_rescope_initial_scope_in_prompt() -> None:
    # 1차 스코프가 프롬프트에 직렬화돼 모델이 "무엇이 빗나갔나" 를 본다.
    llm = _ScriptedLLM({"reasoning": "x", "queries": [
        {"query_text": "q", "collection": "RG"}]})
    rescoper = RescopeLlm(llm=llm, source=_FakeSource())
    await _rescope(rescoper, initial_scope={"filters": {"collection": ["SRP"]}})
    assert "SRP" in llm.last_prompt


@pytest.mark.asyncio
async def test_rescope_llm_unavailable_is_fallback_empty() -> None:
    llm = _ScriptedLLM(fail=True)
    out = await _rescope(RescopeLlm(llm=llm, source=_FakeSource()))
    assert out["method"] == "fallback"
    assert out["queries"] == []


@pytest.mark.asyncio
async def test_rescope_malformed_json_is_fallback_empty() -> None:
    llm = _ScriptedLLM(raw="not json at all")
    out = await _rescope(RescopeLlm(llm=llm, source=_FakeSource()))
    assert out["method"] == "fallback"
    assert out["queries"] == []


@pytest.mark.asyncio
async def test_rescope_skips_queries_without_text() -> None:
    # query_text 없는 항목은 skip.
    llm = _ScriptedLLM({"reasoning": "x", "queries": [
        {"query_text": "", "collection": "RG"},
        {"query_text": "valid one", "collection": "RG", "collection_mode": "filter"},
    ]})
    out = await _rescope(RescopeLlm(llm=llm, source=_FakeSource()))
    assert len(out["queries"]) == 1
    assert out["queries"][0]["query_text"] == "valid one"
