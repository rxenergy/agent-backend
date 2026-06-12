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


# ── Tool calling (agentic Finder variant) ──────────────────────────────────
# 신규 agentic Finder variant 의 Retrieval 단계에서 Finder Agent 가 OpenAI v1
# 프로토콜로 도구를 호출하기 위한 *중립* 도메인 타입(설계 §3, SDK-free). 포트
# 인터페이스는 OpenAI/Anthropic 와이어 타입이 아닌 dataclass 로 정의하고, 두 와이어
# 포맷 변환은 HttpLLM 어댑터 내부에 가둔다(원칙 #4: domain/ports 는 외부 SDK 미import).

# 역할 어휘 — 두 provider 공통 최소 집합.
Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """중립 도구 정의. JSON Schema(draft 2020-12 subset)로 인자를 기술한다.
    `parameters` 는 `{"type":"object","properties":{...},"required":[...]}` 형태."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """모델이 요청한 1개 도구 호출. `arguments` 는 항상 *파싱된 dict* 다 — OpenAI 는
    JSON 문자열로 오므로 어댑터가 `json.loads`, Anthropic 은 이미 dict 다."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatMessage:
    """중립 멀티턴 메시지.

      - system/user: `content`(text)만.
      - assistant: `content` + (선택)`tool_calls` — 모델이 직전에 호출 요청한 것을
        루프가 되돌려준다.
      - tool: `tool_call_id` + `content`(도구 실행 결과). `is_error` 로 실패 표시.
    """

    role: Role
    content: str = ""
    tool_calls: tuple["ToolCall", ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False


# tool_choice 중립 표현:
#   "auto" | "required"(≥1 강제) | "none" | "tool:<name>"(특정 도구 강제)
ToolChoice = str


@dataclass(frozen=True)
class LLMToolResult:
    """`generate_with_tools` 1턴의 결과(non-streaming)."""

    text: str  # 도구 호출과 함께/대신 나온 자연어(있을 수 있음).
    tool_calls: tuple["ToolCall", ...]
    stop_reason: str  # 정규화: "tool_calls" | "stop" | "length" | ...
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

    # 신규 — non-streaming, 도구 없는 멀티메시지 생성 1회. `generate` 는 단일 user
    # 문자열만 받아 system role 을 못 싣고, `generate_with_tools` 는 `tools` 가 필수다.
    # 그 사이를 메우는 경로 — system+user(+이력) 메시지에 structured output(grammar)만
    # 거는 호출자(예: 참조 추출 structured JSON)가 쓴다. tools 가 필요 없으므로
    # `generate_with_tools` 와 구분된다. 어댑터는 messages→wire 변환 + grammar 적용만
    # 책임진다(원칙 #4: domain/ports 는 외부 SDK 미import).
    async def generate_messages(
        self,
        messages: list[ChatMessage],
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> LLMResult: ...

    # 신규 — non-streaming 도구 호출 1턴. 기존 prompt-only 호출자(분류기·생성)는
    # 불변(원칙 #3). 멀티턴 agentic 루프는 어댑터가 아니라 Finder(application)가
    # 소유한다 — 어댑터는 "messages+tools → (text, tool_calls, stop_reason)" 1회만
    # 책임진다(설계 §2). `parallel_tool_calls=False` 기본: Finder 가 도구 순서
    # (scope→normalize→search)를 제어하고 라운드별로 계측하므로 1턴 1도구가 기본.
    async def generate_with_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        tool_choice: ToolChoice = "auto",
        model_options: dict[str, Any] | None = None,
        parallel_tool_calls: bool = False,
    ) -> LLMToolResult: ...
