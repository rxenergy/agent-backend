from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.application.intake.query_translate import QueryTranslator
from app.application.prompting.query_translate_source import QueryTranslatePromptSource
from app.application.prompting.local_source import PromptRegistryError
from app.ports.llm import LLMResult

# N0 — 질의 번역(Intake). 워크플로우 내부는 영어(query_en), 최종 출력만 사용자 언어.
# 표현=모델(번역), 실패 시 결정론 fallback(원문 passthrough, method 기록). 프롬프트는
# registry 호스팅(sha 핀).

_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


class _TransLLM:
    """query_translate JSON 을 돌려주는 controllable fake."""

    model_id = "trans-fake"

    def __init__(self, *, text: str) -> None:
        self._text = text

    async def generate(self, prompt, *, model_options=None, grammar=None):
        return LLMResult(text=self._text, token_usage={"prompt_tokens": 1, "completion_tokens": 1},
                         model_id=self.model_id)

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):  # pragma: no cover
        raise NotImplementedError


class _BrokenLLM:
    """generate 가 터지는 fake — 모델 미가용 경로(graceful fallback)."""

    model_id = "broken"

    async def generate(self, prompt, *, model_options=None, grammar=None):
        raise RuntimeError("upstream down")

    async def generate_stream(self, prompt, *, model_options=None, grammar=None):  # pragma: no cover
        raise NotImplementedError


def _translator(llm) -> QueryTranslator:
    return QueryTranslatePromptSource(_REPO_PROMPTS).build_translator(llm)


@pytest.mark.asyncio
async def test_model_path_translates_to_english() -> None:
    llm = _TransLLM(text=json.dumps({
        "query_en": "What does 10 CFR 50.46 require for ECCS performance?",
        "source_language": "Korean",
    }))
    out = await _translator(llm).translate("10 CFR 50.46 은 ECCS 성능에 무엇을 요구하나?")
    assert out.instantiation_method == "llm"
    assert out.query_en == "What does 10 CFR 50.46 require for ECCS performance?"
    assert out.source_language == "Korean"
    assert out.policy_hash  # 번역 정책 핀(재현).


@pytest.mark.asyncio
async def test_unparseable_output_falls_back_to_passthrough() -> None:
    llm = _TransLLM(text="죄송하지만 JSON 이 아닙니다")
    out = await _translator(llm).translate("원문 질의")
    assert out.instantiation_method == "fallback"
    assert out.query_en == "원문 질의"   # 원문 passthrough(번역 없음).
    assert out.source_language == "Korean"  # 못 읽으면 기본값.
    assert out.policy_hash  # 정책 핀은 fallback 에도 실린다.


@pytest.mark.asyncio
async def test_unavailable_llm_falls_back_not_raises() -> None:
    out = await _translator(_BrokenLLM()).translate("원문")
    assert out.instantiation_method == "fallback"
    assert out.query_en == "원문"


@pytest.mark.asyncio
async def test_empty_query_en_falls_back() -> None:
    llm = _TransLLM(text=json.dumps({"query_en": "", "source_language": "English"}))
    out = await _translator(llm).translate("질의")
    assert out.instantiation_method == "fallback"


def test_source_sha_mismatch_fails_fast(tmp_path) -> None:
    # registry 가 가리키는 fragment 변조 시 boot 거부(무단 편집 차단, 원칙 5).
    import shutil
    dst = tmp_path / "prompts"
    shutil.copytree(_REPO_PROMPTS, dst)
    (dst / "query_translate" / "query_translate_v1.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(PromptRegistryError):
        QueryTranslatePromptSource(dst)
