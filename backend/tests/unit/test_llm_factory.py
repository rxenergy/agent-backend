from __future__ import annotations

import pytest

from app.adapters.llm.fake import FakeEchoLLM
from app.adapters.llm.http import HttpLLM
from app.config.profiles import _build_llm_pool
from app.config.settings import LLMPoolEntry, Settings


def test_pool_always_contains_fake_echo():
    pool = _build_llm_pool(Settings())
    assert "fake-echo" in pool
    assert isinstance(pool["fake-echo"], FakeEchoLLM)


def test_pool_adds_openai_compat_entry():
    s = Settings(
        llm_pool=[
            LLMPoolEntry(
                id="gemma-2-9b",
                provider="openai_compat",
                endpoint="http://vllm:8000/v1",
                model="google/gemma-2-9b-it",
            )
        ]
    )
    pool = _build_llm_pool(s)
    assert set(pool.keys()) == {"fake-echo", "gemma-2-9b"}
    assert isinstance(pool["gemma-2-9b"], HttpLLM)
    assert pool["gemma-2-9b"].model_id == "google/gemma-2-9b-it"


def test_pool_adds_anthropic_entry_with_api_key_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-ant-test")
    s = Settings(
        llm_pool=[
            LLMPoolEntry(
                id="claude-haiku-4-5",
                provider="anthropic",
                endpoint="https://api.anthropic.com/v1",
                model="claude-haiku-4-5",
                api_key_env="ANTHROPIC_TEST_KEY",
            )
        ]
    )
    pool = _build_llm_pool(s)
    assert "claude-haiku-4-5" in pool
    assert isinstance(pool["claude-haiku-4-5"], HttpLLM)


def test_pool_rejects_duplicate_fake_echo_id():
    s = Settings(
        llm_pool=[
            LLMPoolEntry(
                id="fake-echo",
                provider="openai_compat",
                endpoint="http://x",
                model="y",
            )
        ]
    )
    with pytest.raises(ValueError):
        _build_llm_pool(s)
