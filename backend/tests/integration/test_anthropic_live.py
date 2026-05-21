"""Opt-in live Anthropic smoke test for HttpLLM.

Runs only when ANTHROPIC_API_KEY is set. Verifies that the adapter can
reach api.anthropic.com and return a non-empty completion via the Messages
API. Costs a single short Haiku call per run.
"""

from __future__ import annotations

import os

import pytest

from app.adapters.llm.http import HttpLLM

pytestmark = [pytest.mark.anthropic_live, pytest.mark.integration]


@pytest.fixture
def anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set; live Anthropic test skipped")
    return key


async def test_httpllm_anthropic_smoke(anthropic_key: str):
    llm = HttpLLM(
        provider="anthropic",
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5",
        api_key=anthropic_key,
        timeout_s=30.0,
        max_attempts=1,
    )
    result = await llm.generate(
        "Reply with exactly the word OK.",
        model_options={"max_tokens": 16, "temperature": 0.0},
    )
    assert result.text.strip(), "expected non-empty completion"
    assert result.model_id, "expected model_id in result"
