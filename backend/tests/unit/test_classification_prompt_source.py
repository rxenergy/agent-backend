from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.application.prompting.classification_source import ClassificationPromptSource
from app.application.prompting.local_source import PromptRegistryError
from app.ports.llm import LLMResult

# 실제 repo 의 prompts/ — registry sha 핀이 디스크 fragment 와 일치하는지(boot
# fail-fast 불변식) 단위에서 검증한다. backend/tests/unit/ → repo root/prompts.
_REAL_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


@dataclass
class _StubLLM:
    text: str

    async def generate(self, prompt: str, *, model_options=None):
        return LLMResult(
            text=self.text,
            token_usage={"prompt_tokens": 1, "completion_tokens": 1},
            model_id="stub",
        )


def test_real_registry_classification_prompt_sha_matches_disk() -> None:
    # sha 불일치면 PromptRegistryError 로 boot 가 죽는다(무단 편집 차단).
    src = ClassificationPromptSource(_REAL_PROMPTS_DIR)
    assert "{query}" in src.prompt_body
    assert "scope_tier" in src.prompt_body  # intent/scope 축이 프롬프트에 존재
    assert src.model_options.get("max_tokens")  # model_options 외부화 확인
    assert len(src.policy_hash) == 16


def test_missing_classification_block_fails_fast(tmp_path: Path) -> None:
    (tmp_path / "registry.yaml").write_text("prompt_profiles: {}\n", encoding="utf-8")
    with pytest.raises(PromptRegistryError):
        ClassificationPromptSource(tmp_path)


@pytest.mark.asyncio
async def test_build_classifier_parses_intent_and_scope_tier() -> None:
    src = ClassificationPromptSource(_REAL_PROMPTS_DIR)
    llm = _StubLLM(
        text='{"object":"O2","depth":"D3","intent":"definition",'
        '"scope_tier":"T1","object_confidence":0.9,"depth_confidence":0.8}'
    )
    clf = src.build_classifier(llm)
    r = await clf.classify("RG 1.157 원문 정의")
    assert r.scenario_object == "O2"
    assert r.intent == "definition"
    assert r.scope_tier == "T1"
    # 분류 정책 핀 = registry 프롬프트 sha16.
    assert r.classifier_policy_hash == src.policy_hash


@pytest.mark.asyncio
async def test_invalid_intent_and_scope_downgrade_to_unknown() -> None:
    src = ClassificationPromptSource(_REAL_PROMPTS_DIR)
    llm = _StubLLM(
        text='{"object":"O1","depth":"D2","intent":"bogus",'
        '"scope_tier":"T9","object_confidence":0.7,"depth_confidence":0.7}'
    )
    r = await src.build_classifier(llm).classify("NuScale 설계")
    assert r.scenario_object == "O1"
    assert r.intent == "unknown"
    assert r.scope_tier == "unknown"
