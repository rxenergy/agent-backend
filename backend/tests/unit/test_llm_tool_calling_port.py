from __future__ import annotations

import pytest

from app.adapters.llm.fake import FakeEchoLLM, FakeToolLLM
from app.ports.llm import (
    ChatMessage,
    LLMToolResult,
    ToolCall,
    ToolSpec,
)

# T-1 — 포트 중립 타입 + generate_with_tools 시그니처 + FakeToolLLM(스크립트 반환).
# 어댑터 와이어 변환(T-2/T-3)과 독립적으로, 루프가 소비할 중립 계약을 고정한다.


_SCOPE_SPEC = ToolSpec(
    name="retrieval.scope",
    description="검색 범위 파라미터 생성",
    parameters={"type": "object", "properties": {}, "required": []},
)
_VERDICT_SPEC = ToolSpec(
    name="submit_verdict",
    description="종료/충분성 판정 캡처",
    parameters={
        "type": "object",
        "properties": {
            "sufficient": {"type": "boolean"},
            "missing_slots": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["sufficient", "reason"],
    },
)


def _result(*, text="", tool_calls=(), stop_reason="tool_calls") -> LLMToolResult:
    return LLMToolResult(
        text=text,
        tool_calls=tuple(tool_calls),
        stop_reason=stop_reason,
        token_usage={"prompt_tokens": 1, "completion_tokens": 1},
        model_id="fake-tool",
    )


@pytest.mark.asyncio
async def test_fake_tool_llm_returns_script_in_order() -> None:
    call1 = _result(
        tool_calls=(ToolCall("c1", "retrieval.search", {"q": "i-SMR ECCS"}),)
    )
    call2 = _result(
        text="",
        tool_calls=(
            ToolCall("c2", "submit_verdict", {"sufficient": True, "reason": "ok"}),
        ),
        stop_reason="tool_calls",
    )
    llm = FakeToolLLM(script=[call1, call2])

    r1 = await llm.generate_with_tools(
        [ChatMessage(role="user", content="질의")],
        tools=[_SCOPE_SPEC, _VERDICT_SPEC],
        tool_choice="required",
    )
    r2 = await llm.generate_with_tools(
        [ChatMessage(role="user", content="질의")],
        tools=[_SCOPE_SPEC, _VERDICT_SPEC],
        tool_choice="required",
    )

    assert r1 is call1
    assert r2 is call2
    assert llm.calls == 2
    assert r1.tool_calls[0].name == "retrieval.search"
    # arguments 는 항상 파싱된 dict.
    assert r1.tool_calls[0].arguments == {"q": "i-SMR ECCS"}
    assert r2.tool_calls[0].name == "submit_verdict"


@pytest.mark.asyncio
async def test_fake_tool_llm_repeats_last_when_script_exhausted() -> None:
    last = _result(
        tool_calls=(ToolCall("c1", "submit_verdict", {"sufficient": False, "reason": "x"}),)
    )
    llm = FakeToolLLM(script=[last])
    a = await llm.generate_with_tools([], tools=[_VERDICT_SPEC])
    b = await llm.generate_with_tools([], tools=[_VERDICT_SPEC])
    assert a is last and b is last
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_fake_tool_llm_exposes_last_tools_and_messages() -> None:
    llm = FakeToolLLM(script=[_result(stop_reason="stop")])
    msgs = [
        ChatMessage(role="system", content="finder 지시"),
        ChatMessage(role="user", content="질의"),
    ]
    await llm.generate_with_tools(msgs, tools=[_SCOPE_SPEC, _VERDICT_SPEC])
    assert [t.name for t in llm.last_tools] == ["retrieval.scope", "submit_verdict"]
    assert [m.role for m in llm.last_messages] == ["system", "user"]


def test_fake_tool_llm_rejects_empty_script() -> None:
    with pytest.raises(ValueError):
        FakeToolLLM(script=[])


@pytest.mark.asyncio
async def test_fake_echo_tool_path_is_noop_stop() -> None:
    # FakeEchoLLM 은 도구를 안 부르고 즉시 stop — 모델 tools 미지원(설계 §9)과 동형.
    llm = FakeEchoLLM()
    r = await llm.generate_with_tools(
        [ChatMessage(role="user", content="q")],
        tools=[_VERDICT_SPEC],
        tool_choice="required",
    )
    assert r.tool_calls == ()
    assert r.stop_reason == "stop"
    assert [t.name for t in llm.last_tools] == ["submit_verdict"]


@pytest.mark.asyncio
async def test_chat_message_defaults() -> None:
    m = ChatMessage(role="tool", content="결과", tool_call_id="c1", is_error=True)
    assert m.tool_calls == ()
    assert m.is_error is True
    # frozen.
    with pytest.raises(Exception):
        m.content = "x"  # type: ignore[misc]
