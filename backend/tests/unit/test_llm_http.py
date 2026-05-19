from __future__ import annotations

import httpx
import pytest

from app.adapters.llm_http import HttpLLM, LLMUnavailableError


def _mock_transport(handler):
    return httpx.MockTransport(handler)


async def _patched_generate(monkeypatch, llm: HttpLLM, transport: httpx.MockTransport):
    """Patch httpx.AsyncClient so the adapter uses our MockTransport."""
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr("app.adapters.llm_http.httpx.AsyncClient", factory)


async def test_openai_compat_parses_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "model": "gemma-4-it",
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1", model="gemma-4-it")
    await _patched_generate(monkeypatch, llm, _mock_transport(handler))
    result = await llm.generate("hi")
    assert result.text == "hello"
    assert result.token_usage == {"prompt_tokens": 3, "completion_tokens": 1}
    assert result.model_id == "gemma-4-it"


async def test_anthropic_parses_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages")
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["x-api-key"] == "sk-test"
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "hi there"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    llm = HttpLLM(
        provider="anthropic",
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5",
        api_key="sk-test",
    )
    await _patched_generate(monkeypatch, llm, _mock_transport(handler))
    result = await llm.generate("hi")
    assert result.text == "hi there"
    assert result.token_usage == {"prompt_tokens": 5, "completion_tokens": 2}


async def test_4xx_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1", model="x", max_attempts=1)
    await _patched_generate(monkeypatch, llm, _mock_transport(handler))
    with pytest.raises(LLMUnavailableError):
        await llm.generate("hi")


async def test_5xx_retried_then_unavailable(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1", model="x", max_attempts=2)
    await _patched_generate(monkeypatch, llm, _mock_transport(handler))
    with pytest.raises(LLMUnavailableError):
        await llm.generate("hi")
    assert calls["n"] == 2


def test_empty_endpoint_rejected():
    with pytest.raises(ValueError):
        HttpLLM(provider="openai_compat", endpoint="", model="x")
