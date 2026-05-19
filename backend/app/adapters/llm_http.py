from __future__ import annotations

from typing import Any, Literal

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.ports.llm import LLMPort, LLMResult, LLMUnavailableError

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


class _Retry5xx(Exception):
    pass


def _raise_for_status(resp: httpx.Response) -> None:
    if 500 <= resp.status_code < 600:
        raise _Retry5xx(f"upstream {resp.status_code}: {resp.text[:256]}")
    if resp.status_code >= 400:
        # 4xx is a permanent failure (bad request / auth) — surface as unavailable.
        raise LLMUnavailableError(f"upstream {resp.status_code}: {resp.text[:256]}")
