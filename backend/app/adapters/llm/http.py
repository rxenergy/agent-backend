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
        """Stream tokens for OpenAI-compatible providers. Anthropic falls back
        to a single delta wrapping `generate()` (Phase 2 will add native
        Anthropic streaming with thinking blocks)."""
        if self._provider != "openai_compat":
            async for d in _wrap_generate_as_stream(self, prompt, model_options=model_options):
                yield d
            return

        opts = dict(model_options or {})
        max_tokens = int(opts.pop("max_tokens", 1024))
        temperature = float(opts.pop("temperature", 0.0))

        # Retry only the connection establishment; once the first chunk
        # arrives we stream straight through.
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
                    async for delta in self._stream_openai_compat(prompt, max_tokens, temperature):
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


async def _wrap_generate_as_stream(
    llm: LLMPort,
    prompt: str,
    *,
    model_options: dict[str, Any] | None = None,
) -> AsyncIterator[LLMTokenDelta]:
    """Default streaming shim — emit one content delta + one terminal delta
    by calling the blocking `generate()`. Adapters without native streaming
    (Anthropic Phase 1, FakeEcho) reuse this so callers can program against
    a single API."""
    result = await llm.generate(prompt, model_options=model_options)
    if result.text:
        yield LLMTokenDelta(content=result.text)
    yield LLMTokenDelta(
        finish_reason="stop",
        token_usage=dict(result.token_usage),
        model_id=result.model_id,
    )


class _Retry5xx(Exception):
    pass


def _raise_for_status(resp: httpx.Response) -> None:
    if 500 <= resp.status_code < 600:
        raise _Retry5xx(f"upstream {resp.status_code}: {resp.text[:256]}")
    if resp.status_code >= 400:
        # 4xx is a permanent failure (bad request / auth) — surface as unavailable.
        raise LLMUnavailableError(f"upstream {resp.status_code}: {resp.text[:256]}")
