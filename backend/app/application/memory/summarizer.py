from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.domain.interaction import ChatTurn
from app.ports.llm import LLMPort, LLMUnavailableError

_PROMPT = """\
다음 대화 기록을 핵심만 한국어로 요약한다. 사용자가 무엇을 다뤘고 어떤
객체/규제/RAI/노형을 언급했는지 사실 위주로 적는다. 추측·평가 금지.
3~5문장으로 작성한다.

[이전 요약]
{prior}

[새 대화]
{turns}
"""


@dataclass(frozen=True)
class SummarizationResult:
    summary: str
    compressed: bool
    reason: str  # "disabled" / "within_window" / "compressed" / "llm_unavailable"


class ConversationSummarizer:
    """Multi-turn 대화에서 keep_turns를 초과하는 과거 턴을 LLM으로 압축.

    keep_turns 이내면 압축하지 않고 prior summary를 그대로 반환한다.
    """

    def __init__(
        self,
        *,
        llm: LLMPort,
        enabled: bool = True,
        keep_turns: int = 5,
    ) -> None:
        self._llm = llm
        self._enabled = enabled
        self._keep = keep_turns

    async def summarize(
        self,
        *,
        prior_summary: str | None,
        chat_history: Sequence[ChatTurn],
    ) -> SummarizationResult:
        prior = (prior_summary or "").strip()
        if not self._enabled:
            return SummarizationResult(summary=prior, compressed=False, reason="disabled")
        if len(chat_history) <= self._keep:
            return SummarizationResult(summary=prior, compressed=False, reason="within_window")

        # 오래된 턴들 → 압축 대상. 직전 keep_turns는 유지.
        old = list(chat_history[: -self._keep])
        turns_text = "\n".join(f"[{t.role}] {t.content}" for t in old)
        prompt = _PROMPT.replace("{prior}", prior or "(없음)").replace("{turns}", turns_text)
        try:
            result = await self._llm.generate(
                prompt, model_options={"temperature": 0.1, "max_tokens": 400}
            )
        except LLMUnavailableError:
            return SummarizationResult(
                summary=prior, compressed=False, reason="llm_unavailable"
            )
        new_summary = result.text.strip()
        return SummarizationResult(summary=new_summary, compressed=True, reason="compressed")
