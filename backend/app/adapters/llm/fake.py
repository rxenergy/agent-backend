from __future__ import annotations

from typing import Any, AsyncIterator

from app.ports.llm import (
    ChatMessage,
    GrammarSpec,
    LLMPort,
    LLMResult,
    LLMTokenDelta,
    LLMToolResult,
    ToolChoice,
    ToolSpec,
)


class FakeEchoLLM(LLMPort):
    def __init__(self, model_id: str = "fake-echo") -> None:
        self._model_id = model_id
        # Test inspection: the last `grammar` argument observed. Lets tests
        # assert the runner wired schema-constrained decoding through to
        # the LLM without booting a real engine.
        self.last_grammar: GrammarSpec | None = None
        # Test inspection for the tool-calling path (parity with last_grammar).
        self.last_tools: list[ToolSpec] = []
        self.last_messages: list[ChatMessage] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def generate(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> LLMResult:
        self.last_grammar = grammar
        text = f"[fake-echo] {prompt[-512:]}"
        return LLMResult(
            text=text,
            token_usage={"prompt_tokens": len(prompt), "completion_tokens": len(text)},
            model_id=self._model_id,
        )

    async def generate_stream(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> AsyncIterator[LLMTokenDelta]:
        result = await self.generate(
            prompt, model_options=model_options, grammar=grammar
        )
        if result.text:
            yield LLMTokenDelta(content=result.text)
        yield LLMTokenDelta(
            finish_reason="stop",
            token_usage=dict(result.token_usage),
            model_id=result.model_id,
        )

    async def generate_with_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        tool_choice: ToolChoice = "auto",
        model_options: dict[str, Any] | None = None,
        parallel_tool_calls: bool = False,
    ) -> LLMToolResult:
        """Protocol 완결용 no-op — 도구를 부르지 않고 즉시 stop 한다. Finder 루프를
        실제로 태우는 단위 테스트는 `FakeToolLLM`(스크립트 주입)을 쓴다. 여기서 빈
        tool_calls + stop 을 돌려주는 것은, 모델이 `tools` 를 미지원할 때(설계 §9)와
        동형이라 `max_turns` backstop 경로를 단위 fixture 없이도 노출한다."""
        self.last_tools = list(tools)
        self.last_messages = list(messages)
        return LLMToolResult(
            text="",
            tool_calls=(),
            stop_reason="stop",
            token_usage={"prompt_tokens": 0, "completion_tokens": 0},
            model_id=self._model_id,
        )


class FakeToolLLM(LLMPort):
    """도구 호출 루프 단위 테스트용 controllable fake(설계 §6).

    `script` 의 항목을 `generate_with_tools` 호출마다 순서대로 반환한다 → Finder
    루프·recover·계측을 컨테이너 없이 검증한다. 스크립트가 소진되면 마지막 항목을
    반복(backstop 경로 안정). `last_tools`/`last_messages` 로 직렬화 입력을 노출해
    어댑터 변환과 무관하게 루프가 보낸 중립 타입을 단언할 수 있게 한다.

    prompt-only 경로(`generate`/`generate_stream`)도 Protocol 완결을 위해 구현하되,
    이 fake 의 주 용도는 도구 호출 루프다.
    """

    def __init__(
        self,
        *,
        script: list[LLMToolResult],
        model_id: str = "fake-tool",
    ) -> None:
        if not script:
            raise ValueError("FakeToolLLM requires a non-empty script")
        self._model_id = model_id
        self._script = list(script)
        self._cursor = 0
        self.calls = 0
        self.last_tools: list[ToolSpec] = []
        self.last_messages: list[ChatMessage] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def generate(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> LLMResult:
        return LLMResult(
            text="[fake-tool] " + prompt[-256:],
            token_usage={"prompt_tokens": len(prompt), "completion_tokens": 0},
            model_id=self._model_id,
        )

    async def generate_stream(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> AsyncIterator[LLMTokenDelta]:
        result = await self.generate(
            prompt, model_options=model_options, grammar=grammar
        )
        if result.text:
            yield LLMTokenDelta(content=result.text)
        yield LLMTokenDelta(
            finish_reason="stop",
            token_usage=dict(result.token_usage),
            model_id=result.model_id,
        )

    async def generate_with_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        tool_choice: ToolChoice = "auto",
        model_options: dict[str, Any] | None = None,
        parallel_tool_calls: bool = False,
    ) -> LLMToolResult:
        self.calls += 1
        self.last_tools = list(tools)
        self.last_messages = list(messages)
        idx = min(self._cursor, len(self._script) - 1)
        self._cursor += 1
        return self._script[idx]
