from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LLMUnavailableError(RuntimeError):
    """Raised by LLM adapters when the upstream endpoint is unreachable or
    returns a non-retryable failure. The runner maps this to
    refusal_reason='llm_unavailable'."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    token_usage: dict[str, int]
    model_id: str


class LLMPort(Protocol):
    async def generate(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
    ) -> LLMResult: ...
