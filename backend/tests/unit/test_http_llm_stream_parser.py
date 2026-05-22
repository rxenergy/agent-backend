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


_ANTHROPIC_STREAM_LINES = b"""\
event: message_start
data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"claude-opus-4-7","content":[],"stop_reason":null,"usage":{"input_tokens":42,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Let me think."}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"EqQB..."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}

event: ping
data: {"type":"ping"}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"The answer "}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"is 42."}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":7}}

event: message_stop
data: {"type":"message_stop"}

"""


@pytest.mark.asyncio
async def test_anthropic_stream_parses_thinking_text_and_usage(monkeypatch):
    """Anthropic SSE has its own wire format (event:+data: pairs, no [DONE]).
    Thinking is a separate content block emitting `thinking_delta`; text
    follows. `message_delta.usage.output_tokens` is cumulative-final.
    `signature_delta` and `ping` events are intentionally dropped."""
    llm = HttpLLM(
        provider="anthropic",
        endpoint="https://api.anthropic.com/v1",
        model="claude-opus-4-7",
        api_key="sk-test",
        max_attempts=1,
    )
    await _patch_client(monkeypatch, _stream_transport(_ANTHROPIC_STREAM_LINES))

    deltas = [d async for d in llm.generate_stream("hi")]
    contents = [d.content for d in deltas if d.content]
    reasoning = [d.reasoning for d in deltas if d.reasoning]
    assert contents == ["The answer ", "is 42."]
    assert reasoning == ["Let me think."]

    terminal = deltas[-1]
    # Anthropic `end_turn` → OpenAI-compat `stop`.
    assert terminal.finish_reason == "stop"
    assert terminal.token_usage == {
        "prompt_tokens": 42,
        "completion_tokens": 7,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assert terminal.model_id == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_anthropic_stream_request_payload(monkeypatch):
    """Opus 4.7 must not receive `temperature`; adaptive thinking with
    summarized display must be auto-attached so reasoning is visible."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content.decode())
        return httpx.Response(
            200,
            content=_ANTHROPIC_STREAM_LINES,
            headers={"content-type": "text/event-stream"},
        )

    llm = HttpLLM(
        provider="anthropic",
        endpoint="https://api.anthropic.com/v1",
        model="claude-opus-4-7",
        api_key="sk-test",
        max_attempts=1,
    )
    await _patch_client(monkeypatch, httpx.MockTransport(handler))
    async for _ in llm.generate_stream("hi"):
        pass

    body = captured["body"]
    assert body["stream"] is True
    assert "temperature" not in body  # Opus 4.7 rejects sampling params
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}


@pytest.mark.asyncio
async def test_anthropic_stream_no_thinking_for_haiku(monkeypatch):
    """Haiku 4.5 does not support adaptive thinking — must NOT auto-attach
    the `thinking` field, and `temperature` should still be present since
    Opus-4.7's removal doesn't apply."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content.decode())
        return httpx.Response(
            200,
            content=_ANTHROPIC_STREAM_LINES,
            headers={"content-type": "text/event-stream"},
        )

    llm = HttpLLM(
        provider="anthropic",
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5",
        api_key="sk-test",
        max_attempts=1,
    )
    await _patch_client(monkeypatch, httpx.MockTransport(handler))
    async for _ in llm.generate_stream("hi"):
        pass

    body = captured["body"]
    assert "thinking" not in body
    assert "temperature" in body
