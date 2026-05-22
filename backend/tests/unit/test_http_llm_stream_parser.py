"""SSE parser exercised by HttpLLM.generate_stream (openai_compat provider).

We feed a canned `data: …` line stream through httpx.MockTransport and assert
the adapter emits the expected `LLMTokenDelta` sequence: content tokens, an
optional reasoning_content token (DeepSeek-R1 convention), and a terminal
delta carrying finish_reason + usage.
"""
from __future__ import annotations

import httpx
import pytest

from app.adapters.llm.http import HttpLLM, LLMUnavailableError


_OPENAI_STREAM_LINES = b"""\
data: {"id":"x","model":"gemma-stream","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"x","model":"gemma-stream","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}

data: {"id":"x","model":"gemma-stream","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}

data: {"id":"x","model":"gemma-stream","choices":[{"index":0,"delta":{"reasoning_content":"because"},"finish_reason":null}]}

data: {"id":"x","model":"gemma-stream","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"x","model":"gemma-stream","choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}

data: [DONE]

"""


def _stream_transport(body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})
    return httpx.MockTransport(handler)


async def _patch_client(monkeypatch, transport: httpx.MockTransport) -> None:
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr("app.adapters.llm.http.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_openai_compat_stream_parses_content_reasoning_and_usage(monkeypatch):
    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1",
                  model="gemma-stream", max_attempts=1)
    await _patch_client(monkeypatch, _stream_transport(_OPENAI_STREAM_LINES))

    deltas = []
    async for d in llm.generate_stream("hi"):
        deltas.append(d)

    contents = [d.content for d in deltas if d.content]
    assert contents == ["Hel", "lo"]
    reasoning = [d.reasoning for d in deltas if d.reasoning]
    assert reasoning == ["because"]
    # Terminal delta has finish_reason + usage + model_id.
    terminal = deltas[-1]
    assert terminal.finish_reason == "stop"
    assert terminal.token_usage == {"prompt_tokens": 4, "completion_tokens": 2}
    assert terminal.model_id == "gemma-stream"


@pytest.mark.asyncio
async def test_openai_compat_stream_4xx_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1",
                  model="x", max_attempts=1)
    await _patch_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(LLMUnavailableError):
        async for _ in llm.generate_stream("hi"):
            pass


@pytest.mark.asyncio
async def test_anthropic_provider_falls_back_to_wrap(monkeypatch):
    """Anthropic streaming is Phase 2 — adapter wraps generate() into a
    single content + terminal delta so the runner can program against a
    single API."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    llm = HttpLLM(provider="anthropic",
                  endpoint="https://api.anthropic.com/v1",
                  model="claude-haiku-4-5", api_key="sk-test")
    await _patch_client(monkeypatch, httpx.MockTransport(handler))
    deltas = [d async for d in llm.generate_stream("hi")]
    assert any(d.content == "ok" for d in deltas)
    assert deltas[-1].finish_reason == "stop"
