from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.application.intake.answer_spec import AnswerSpecInstantiator
from app.application.prompting.answer_spec_source import AnswerSpecPromptSource
from app.application.prompting.local_source import PromptRegistryError
from app.ports.llm import LLMResult

# F-2 — N2 answer_spec 모델 인스턴스화. 표현=모델(슬롯 산출), 실패 시 결정론 fallback
# (method 기록, silent degrade 금지). 프롬프트는 registry 호스팅(sha 핀).

_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


class _SpecLLM:
    """answer_spec JSON 을 돌려주는 controllable fake."""

    model_id = "spec-fake"

    def __init__(self, *, text: str) -> None:
        self._text = text

    async def generate(self, prompt, *, model_options=None, grammar=None):
        return LLMResult(text=self._text, token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):  # pragma: no cover
        raise NotImplementedError


def _instantiator(llm) -> AnswerSpecInstantiator:
    src = AnswerSpecPromptSource(_REPO_PROMPTS)
    return src.build_instantiator(llm)


@pytest.mark.asyncio
async def test_model_path_produces_slots_and_structure() -> None:
    llm = _SpecLLM(text=json.dumps({
        "required_slots": [
            {"name": "governing_clause", "description": "지배 조문", "required": True},
            {"name": "design_feature", "required": False},
        ],
        "answer_structure": "지배조문→요건",
    }))
    spec = await _instantiator(llm).instantiate(
        "i-SMR ECCS 요건", scenario_object="O4", scenario_depth="D2",
        intent="compliance", entities={},
    )
    assert spec.instantiation_method == "llm"
    assert [s.name for s in spec.required_slots] == ["governing_clause", "design_feature"]
    assert spec.required_slots[1].required is False
    assert spec.answer_structure == "지배조문→요건"
    assert spec.depth == "D2"  # 분류 깊이 passthrough.
    assert spec.spec_hash and spec.policy_hash  # 산출 fingerprint + 정책 핀.


@pytest.mark.asyncio
async def test_unparseable_output_falls_back_deterministically() -> None:
    llm = _SpecLLM(text="죄송하지만 JSON 이 아닙니다")
    spec = await _instantiator(llm).instantiate(
        "질의", scenario_object="O4", scenario_depth="D2",
        intent="compliance", entities={},
    )
    assert spec.instantiation_method == "fallback"
    # compliance prior 슬롯이 실린다.
    assert "governing_clause" in {s.name for s in spec.required_slots}
    assert spec.policy_hash  # 정책 핀은 fallback 에도 실린다.


@pytest.mark.asyncio
async def test_empty_slots_output_falls_back() -> None:
    llm = _SpecLLM(text=json.dumps({"required_slots": [], "answer_structure": "x"}))
    spec = await _instantiator(llm).instantiate(
        "질의", scenario_object="O4", scenario_depth="D2", intent="definition", entities={},
    )
    # 빈 슬롯은 유효 사양이 아니다 → fallback(definition prior).
    assert spec.instantiation_method == "fallback"
    assert "definition" in {s.name for s in spec.required_slots}


def test_source_sha_mismatch_fails_fast(tmp_path) -> None:
    # registry 가 가리키는 fragment 를 변조하면 boot 시 sha 불일치로 거부(무단 편집 차단).
    import shutil
    dst = tmp_path / "prompts"
    shutil.copytree(_REPO_PROMPTS, dst)
    (dst / "answer_spec" / "answer_spec_v2.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(PromptRegistryError):
        AnswerSpecPromptSource(dst)
