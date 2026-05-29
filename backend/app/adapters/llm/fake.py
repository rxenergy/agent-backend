from __future__ import annotations

from typing import Any, AsyncIterator

from app.ports.llm import GrammarSpec, LLMPort, LLMResult, LLMTokenDelta


class FakeEchoLLM(LLMPort):
    def __init__(self, model_id: str = "fake-echo") -> None:
        self._model_id = model_id
        # Test inspection: the last `grammar` argument observed. Lets tests
        # assert the runner wired schema-constrained decoding through to
        # the LLM without booting a real engine.
        self.last_grammar: GrammarSpec | None = None

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
