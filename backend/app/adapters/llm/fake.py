from __future__ import annotations

from typing import Any

from app.ports.llm import LLMPort, LLMResult


class FakeEchoLLM(LLMPort):
    def __init__(self, model_id: str = "fake-echo") -> None:
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    async def generate(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
    ) -> LLMResult:
        text = f"[fake-echo] {prompt[-512:]}"
        return LLMResult(
            text=text,
            token_usage={"prompt_tokens": len(prompt), "completion_tokens": len(text)},
            model_id=self._model_id,
        )
