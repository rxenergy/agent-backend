from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


class LLMUnavailableError(RuntimeError):
    """Raised by LLM adapters when the upstream endpoint is unreachable or
    returns a non-retryable failure. The runner maps this to
    refusal_reason='llm_unavailable'."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    token_usage: dict[str, int]
    model_id: str


@dataclass(frozen=True)
class LLMTokenDelta:
    """Single increment from an LLM streaming call.

    `content` carries answer-body tokens (OpenAI `delta.content`).
    `reasoning` carries provider-side chain-of-thought tokens
    (DeepSeek-R1 / OpenAI o-series `delta.reasoning_content`).
    `finish_reason` is set on the terminal delta only.
    `token_usage` is populated by adapters that emit a usage-bearing
    final chunk (vLLM, OpenAI when `stream_options.include_usage=true`).
    `model_id` mirrors the upstream `model` field when present.
    """

    content: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    model_id: str | None = None


class LLMPort(Protocol):
    async def generate(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
    ) -> LLMResult: ...

    def generate_stream(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMTokenDelta]: ...
