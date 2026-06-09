from __future__ import annotations

import json

from app.application.agents.events import LazyReasoning
from app.ports.llm import GrammarSpec, LLMPort, LLMResult

# spec_driven_v1 N1/N2 의 thinking 캡처 공유 헬퍼(설계 spec_driven_thinking_output.design.v1.md
# D2/D3). emitter 활성 시 utility LLM 을 streaming 으로 돌려 native reasoning(reasoning
# 모델)을 LazyReasoning 으로 흘리고, JSON 본문(구조화 출력)은 토큰 미방출·버퍼만 한다
# (#24295: 본문은 N4 답변 토큰이므로 여기선 emit 하지 않는다). native CoT 가 한 건도
# 없으면(소형/Gemma onprem) 호출자가 `extract_reasoning` 으로 구조화 `reasoning` 필드를
# backstop 으로 emit 한다 — 어느 프로파일에서도 앞단 thinking 이 Thought 블록에 닿게.


async def stream_capture(
    llm: LLMPort,
    prompt: str,
    *,
    model_options: dict,
    grammar: GrammarSpec | None,
    lazy: LazyReasoning,
) -> LLMResult:
    """utility LLM 을 streaming 으로 돌려 (native reasoning→lazy, content→버퍼) 후
    LLMResult 로 합친다. 본문 토큰은 방출하지 않는다(구조화 출력은 답이 아니다)."""
    text_buf: list[str] = []
    token_usage: dict[str, int] = {}
    model_id: str | None = None
    async for delta in llm.generate_stream(
        prompt, model_options=model_options, grammar=grammar
    ):
        if delta.content:
            text_buf.append(delta.content)
        if delta.reasoning:
            await lazy.feed(delta.reasoning)
        if delta.token_usage:
            token_usage = dict(delta.token_usage)
        if delta.model_id:
            model_id = delta.model_id
    text = "".join(text_buf)
    return LLMResult(
        text=text,
        token_usage=token_usage
        or {"prompt_tokens": 0, "completion_tokens": len(text)},
        model_id=model_id or getattr(llm, "model_id", "unknown"),
    )


def extract_reasoning(text: str) -> str:
    """구조화 출력에서 선행 `reasoning` 문자열 필드를 관대하게 추출(없으면 "").
    `_parse`(N1/N2)와 동일한 brace 추출 idiom — 깨진 출력은 빈 문자열로 흘려보낸다."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return ""
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("reasoning") or "").strip()
