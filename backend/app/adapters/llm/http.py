from __future__ import annotations

import json
from typing import Any, AsyncIterator, Literal

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.ports.llm import LLMPort, LLMResult, LLMTokenDelta, LLMUnavailableError

__all__ = ["HttpLLM", "LLMUnavailableError"]

Provider = Literal["openai_compat", "anthropic"]


class HttpLLM(LLMPort):
    """OpenAI /v1/chat/completions compatible client (vLLM, OpenAI, LM Studio, Ollama)
    plus Anthropic /v1/messages. Single adapter, provider-switched at construction time.
    """

    def __init__(
        self,
        *,
        provider: Provider,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        max_attempts: int = 2,
        anthropic_version: str = "2023-06-01",
    ) -> None:
        if not endpoint:
            raise ValueError("HttpLLM requires a non-empty endpoint")
        self._provider: Provider = provider
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._anthropic_version = anthropic_version

    @property
    def model_id(self) -> str:
        return self._model

    async def generate(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
    ) -> LLMResult:
        opts = dict(model_options or {})
        max_tokens = int(opts.pop("max_tokens", 1024))
        temperature = float(opts.pop("temperature", 0.0))

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.RemoteProtocolError, _Retry5xx)
                ),
                reraise=True,
            ):
                with attempt:
                    if self._provider == "openai_compat":
                        return await self._call_openai_compat(prompt, max_tokens, temperature)
                    return await self._call_anthropic(prompt, max_tokens, temperature)
        except (httpx.HTTPError, RetryError, _Retry5xx) as exc:
            raise LLMUnavailableError(str(exc)) from exc

        raise LLMUnavailableError("HttpLLM: retry loop exited without result")

    async def generate_stream(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMTokenDelta]:
        """Stream tokens for OpenAI-compatible and Anthropic providers.

        Anthropic SSE differs from OpenAI: events use `event:` + `data:` line
        pairs (no `[DONE]` sentinel), thinking is its own content block
        emitting `thinking_delta`, and `message_delta.usage.output_tokens` is
        cumulative-final. Extended thinking is auto-enabled (adaptive +
        summarized display) on models that support it so the runner can
        surface reasoning as OpenAI-compat `reasoning_content` deltas.
        """
        opts = dict(model_options or {})
        max_tokens = int(opts.pop("max_tokens", 1024))
        temperature = float(opts.pop("temperature", 0.0))

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.RemoteProtocolError, _Retry5xx)
                ),
                reraise=True,
            ):
                with attempt:
                    if self._provider == "openai_compat":
                        async for delta in self._stream_openai_compat(
                            prompt, max_tokens, temperature
                        ):
                            yield delta
                    else:
                        async for delta in self._stream_anthropic(
                            prompt, max_tokens, temperature
                        ):
                            yield delta
                    return
        except (httpx.HTTPError, RetryError, _Retry5xx) as exc:
            raise LLMUnavailableError(str(exc)) from exc

    async def _call_openai_compat(
        self, prompt: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        url = f"{self._endpoint}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
        _raise_for_status(resp)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or choice.get("text") or ""
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            token_usage={
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
            },
            model_id=str(data.get("model") or self._model),
        )

    async def _stream_openai_compat(
        self, prompt: str, max_tokens: int, temperature: float
    ) -> AsyncIterator[LLMTokenDelta]:
        url = f"{self._endpoint}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            # vLLM + OpenAI both honor this — surface usage in the final
            # chunk so the runner can populate token_usage even when
            # streaming.
            "stream_options": {"include_usage": True},
        }

        finish_reason: str | None = None
        model_id_seen: str | None = None
        usage_seen: dict[str, int] = {}

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if 500 <= resp.status_code < 600:
                    body = await resp.aread()
                    raise _Retry5xx(f"upstream {resp.status_code}: {body[:256]!r}")
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise LLMUnavailableError(
                        f"upstream {resp.status_code}: {body[:256]!r}"
                    )

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        # SSE comment / keepalive.
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("model"):
                        model_id_seen = str(chunk["model"])
                    usage = chunk.get("usage") or {}
                    if usage:
                        usage_seen = {
                            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                            "completion_tokens": int(usage.get("completion_tokens", 0)),
                        }

                    choices = chunk.get("choices") or []
                    if not choices:
                        # Some providers emit a usage-only terminal chunk
                        # with empty choices — already captured above.
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    reasoning = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    finish_reason = choice.get("finish_reason") or finish_reason
                    if content or reasoning:
                        yield LLMTokenDelta(
                            content=content,
                            reasoning=reasoning,
                        )

        yield LLMTokenDelta(
            finish_reason=finish_reason or "stop",
            token_usage=usage_seen,
            model_id=model_id_seen or self._model,
        )

    async def _stream_anthropic(
        self, prompt: str, max_tokens: int, temperature: float
    ) -> AsyncIterator[LLMTokenDelta]:
        url = f"{self._endpoint}/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": self._anthropic_version,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        # Opus 4.7 rejects temperature/top_p/top_k with 400; suppress them
        # when targeting 4.7. For other models pass the configured value.
        if not _is_opus_4_7(self._model):
            payload["temperature"] = temperature
        # Auto-enable adaptive thinking with visible summary on models that
        # support it. The runner forwards `thinking_delta` text into the
        # OpenAI-compat `delta.reasoning_content` field so OpenWebUI shows
        # it in its reasoning pane.
        if _supports_adaptive_thinking(self._model):
            payload["thinking"] = {
                "type": "adaptive",
                "display": "summarized",
            }

        # Track per-content-block type so deltas route correctly.
        # Anthropic sends content_block_start with type="thinking" or "text"
        # (and "tool_use" for tools); deltas carry their own `delta.type`
        # but we still need the block type to know whether `thinking_delta`
        # in this block should be surfaced.
        block_types: dict[int, str] = {}
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_create = 0
        finish_reason: str | None = None
        model_id_seen: str | None = None

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if 500 <= resp.status_code < 600:
                    body = await resp.aread()
                    raise _Retry5xx(f"upstream {resp.status_code}: {body[:256]!r}")
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise LLMUnavailableError(
                        f"upstream {resp.status_code}: {body[:256]!r}"
                    )

                async for line in resp.aiter_lines():
                    if not line or line.startswith(":") or line.startswith("event:"):
                        # Anthropic also sends `event:` lines — we route on
                        # `data.type`, so skip them. Blank lines separate
                        # SSE frames.
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        evt = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    etype = evt.get("type")

                    if etype == "message_start":
                        msg = evt.get("message") or {}
                        if msg.get("model"):
                            model_id_seen = str(msg["model"])
                        usage = msg.get("usage") or {}
                        input_tokens = int(usage.get("input_tokens", 0))
                        cache_read = int(usage.get("cache_read_input_tokens", 0))
                        cache_create = int(usage.get("cache_creation_input_tokens", 0))
                    elif etype == "content_block_start":
                        idx = int(evt.get("index", 0))
                        cb = evt.get("content_block") or {}
                        block_types[idx] = str(cb.get("type") or "")
                    elif etype == "content_block_delta":
                        delta = evt.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text = delta.get("text") or ""
                            if text:
                                yield LLMTokenDelta(content=text)
                        elif dtype == "thinking_delta":
                            text = delta.get("thinking") or ""
                            if text:
                                yield LLMTokenDelta(reasoning=text)
                        # signature_delta / input_json_delta intentionally
                        # not forwarded — signature is an opaque attestation
                        # token (only matters when replaying thinking blocks
                        # in a follow-up turn, which the runner doesn't do),
                        # and tool_use streaming isn't wired here.
                    elif etype == "content_block_stop":
                        block_types.pop(int(evt.get("index", -1)), None)
                    elif etype == "message_delta":
                        d = evt.get("delta") or {}
                        if d.get("stop_reason"):
                            finish_reason = str(d["stop_reason"])
                        usage = evt.get("usage") or {}
                        # output_tokens here is cumulative-final — overwrite.
                        if "output_tokens" in usage:
                            output_tokens = int(usage["output_tokens"])
                    elif etype == "message_stop":
                        break
                    elif etype == "error":
                        err = evt.get("error") or {}
                        raise LLMUnavailableError(
                            f"anthropic stream error: {err.get('type')}: "
                            f"{err.get('message')}"
                        )
                    # `ping` and unknown event types are ignored.

        # Map Anthropic stop_reason → OpenAI-compat finish_reason vocabulary
        # so downstream (smr_agent + OpenAI chunk frame) stays consistent.
        finish_mapped = _map_anthropic_stop_reason(finish_reason)
        yield LLMTokenDelta(
            finish_reason=finish_mapped,
            token_usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
            model_id=model_id_seen or self._model,
        )

    async def _call_anthropic(
        self, prompt: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        url = f"{self._endpoint}/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": self._anthropic_version,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
        _raise_for_status(resp)
        data = resp.json()
        parts = data.get("content") or []
        text = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            token_usage={
                "prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
            },
            model_id=str(data.get("model") or self._model),
        )


_ADAPTIVE_THINKING_MODELS = ("claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")


def _supports_adaptive_thinking(model_id: str) -> bool:
    """Adaptive thinking is GA on Opus 4.6/4.7 and Sonnet 4.6. Haiku and
    older snapshots either don't support it or use the legacy `enabled +
    budget_tokens` form, so we don't auto-attach. Match on prefix to absorb
    date-suffix snapshots (e.g. `claude-opus-4-7-20260301`)."""
    return any(model_id.startswith(m) for m in _ADAPTIVE_THINKING_MODELS)


def _is_opus_4_7(model_id: str) -> bool:
    """Opus 4.7 returns 400 if `temperature` / `top_p` / `top_k` are present —
    the sampling parameters were removed there. Match on prefix to absorb
    future date-suffix snapshots."""
    return model_id.startswith("claude-opus-4-7")


def _map_anthropic_stop_reason(reason: str | None) -> str:
    """Anthropic vocabulary → OpenAI vocabulary. `end_turn` is the normal
    completion; `max_tokens` matches; tool/stop-sequence map to their
    OpenAI counterparts."""
    if reason is None:
        return "stop"
    if reason in ("end_turn", "stop_sequence"):
        return "stop"
    if reason == "max_tokens":
        return "length"
    if reason == "tool_use":
        return "tool_calls"
    return reason


class _Retry5xx(Exception):
    pass


def _raise_for_status(resp: httpx.Response) -> None:
    if 500 <= resp.status_code < 600:
        raise _Retry5xx(f"upstream {resp.status_code}: {resp.text[:256]}")
    if resp.status_code >= 400:
        # 4xx is a permanent failure (bad request / auth) — surface as unavailable.
        raise LLMUnavailableError(f"upstream {resp.status_code}: {resp.text[:256]}")
