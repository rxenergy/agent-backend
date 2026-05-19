from __future__ import annotations

import pytest

from app.adapters.llm_fake import FakeEchoLLM
from app.adapters.llm_http import HttpLLM
from app.config.profiles import _build_llm
from app.config.settings import Settings


def test_fake_provider_returns_fake_llm():
    s = Settings(llm_provider="fake")
    llm = _build_llm(s)
    assert isinstance(llm, FakeEchoLLM)


def test_openai_compat_builds_http_llm():
    s = Settings(
        llm_provider="openai_compat",
        llm_endpoint="http://vllm:8000/v1",
        llm_model="google/gemma-2-9b-it",
    )
    llm = _build_llm(s)
    assert isinstance(llm, HttpLLM)
    assert llm.model_id == "google/gemma-2-9b-it"


def test_anthropic_builds_http_llm():
    s = Settings(
        llm_provider="anthropic",
        llm_endpoint="https://api.anthropic.com/v1",
        llm_model="claude-haiku-4-5",
        llm_api_key="sk-ant-test",
    )
    llm = _build_llm(s)
    assert isinstance(llm, HttpLLM)


def test_http_provider_requires_endpoint_and_model():
    s = Settings(llm_provider="openai_compat", llm_endpoint="", llm_model="")
    with pytest.raises(ValueError):
        _build_llm(s)
