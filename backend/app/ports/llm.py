from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol


class LLMUnavailableError(RuntimeError):
    """Raised by LLM adapters when the upstream endpoint is unreachable or
    returns a non-retryable failure. The runner maps this to
    refusal_reason='llm_unavailable'."""


GrammarKind = Literal["grammar", "regex", "json_schema", "choice"]


@dataclass(frozen=True)
class GrammarSpec:
    """Schema-constrained decoding directive (v3.1 §Node 13 hallucination
    defense line 3). Adapters that support guided decoding (vLLM via
    XGrammar/Outlines) enforce `value` at the sampling step so invalid
    tokens never appear in the stream. Adapters without guided-decoding
    support treat the spec as a no-op — the citation-contract prompt
    fragment still steers behaviour, but enforcement falls to the
    downstream Claim verifier.

    `kind`:
      - "grammar"     — GBNF / EBNF source (`value: str`)
      - "regex"       — single regex (`value: str`)
      - "json_schema" — JSON Schema dict (`value: dict`)
      - "choice"      — list of allowed completions (`value: list[str]`)
    """

    kind: GrammarKind
    value: Any


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
        grammar: GrammarSpec | None = None,
    ) -> LLMResult: ...

    def generate_stream(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> AsyncIterator[LLMTokenDelta]: ...
