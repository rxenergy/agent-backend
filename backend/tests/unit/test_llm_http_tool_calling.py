from __future__ import annotations

import json as _json

import httpx
import pytest

from app.adapters.llm.http import HttpLLM
from app.ports.llm import ChatMessage, ToolCall, ToolSpec

# T-2/T-3 — HttpLLM.generate_with_tools 의 요청 직렬화 + 응답 파싱(설계 §4).
# vLLM(openai_compat) 과 Anthropic 양쪽 와이어 포맷을 mock httpx 로 검증한다.


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _patch(monkeypatch, transport):
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr("app.adapters.llm.http.httpx.AsyncClient", factory)


_SCOPE = ToolSpec(
    name="retrieval.scope",
    description="검색 범위",
    parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": []},
)
_VERDICT = ToolSpec(
    name="submit_verdict",
    description="판정",
    parameters={
        "type": "object",
        "properties": {"sufficient": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["sufficient", "reason"],
    },
)


# ── T-2 openai_compat ──────────────────────────────────────────────────────


async def test_openai_serializes_tools_and_choice(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gemma-4",
                "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1", model="gemma-4")
    _patch(monkeypatch, _mock_transport(handler))
    r = await llm.generate_with_tools(
        [ChatMessage(role="user", content="질의")],
        tools=[_SCOPE, _VERDICT],
        tool_choice="required",
    )
    # 직렬화: tools → function 형태, tool_choice 그대로, parallel False.
    # 도구 이름은 와이어에서 `.`→`_` 정규화(provider 이름 패턴 ^[a-zA-Z0-9_-]$).
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "retrieval_scope"
    assert captured["tools"][1]["function"]["parameters"] == _VERDICT.parameters
    assert captured["tool_choice"] == "required"
    assert captured["parallel_tool_calls"] is False
    # 파싱.
    assert r.text == "done"
    assert r.stop_reason == "stop"
    assert r.tool_calls == ()
    assert r.token_usage == {"prompt_tokens": 5, "completion_tokens": 2}


async def test_openai_parses_tool_calls_and_json_loads_arguments(monkeypatch):
    # 모델은 와이어 이름(정규화된 `retrieval_scope`)으로 호출을 돌려준다 —
    # 어댑터가 요청 tools 역매핑으로 원래 점 이름(`retrieval.scope`)으로 복원해야 한다.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gemma-4",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "retrieval_scope",
                                        "arguments": '{"q": "i-SMR ECCS", "k": 5}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {},
            },
        )

    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1", model="gemma-4")
    _patch(monkeypatch, _mock_transport(handler))
    r = await llm.generate_with_tools(
        [ChatMessage(role="user", content="q")], tools=[_SCOPE], tool_choice="required"
    )
    assert r.stop_reason == "tool_calls"
    assert len(r.tool_calls) == 1
    tc = r.tool_calls[0]
    assert tc.id == "call_1"
    # 와이어 `retrieval_scope` → registry 점 이름으로 복원.
    assert tc.name == "retrieval.scope"
    # OpenAI arguments(JSON 문자열) → 파싱된 dict.
    assert tc.arguments == {"q": "i-SMR ECCS", "k": 5}


async def test_openai_specific_tool_choice_serialization(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "x", "choices": [{"finish_reason": "stop", "message": {"content": ""}}], "usage": {}},
        )

    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1", model="x")
    _patch(monkeypatch, _mock_transport(handler))
    await llm.generate_with_tools([], tools=[_VERDICT], tool_choice="tool:submit_verdict")
    assert captured["tool_choice"] == {"type": "function", "function": {"name": "submit_verdict"}}


async def test_openai_serializes_assistant_and_tool_messages(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "x", "choices": [{"finish_reason": "stop", "message": {"content": ""}}], "usage": {}},
        )

    llm = HttpLLM(provider="openai_compat", endpoint="http://vllm/v1", model="x")
    _patch(monkeypatch, _mock_transport(handler))
    msgs = [
        ChatMessage(role="system", content="finder"),
        ChatMessage(role="user", content="질의"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=(ToolCall("c1", "retrieval.search", {"q": "a"}),),
        ),
        ChatMessage(role="tool", content='{"chunks": []}', tool_call_id="c1"),
    ]
    await llm.generate_with_tools(msgs, tools=[_SCOPE])
    wire = captured["messages"]
    assert wire[0] == {"role": "system", "content": "finder"}
    assert wire[2]["role"] == "assistant"
    # 멀티턴 재직렬화: 히스토리의 tool_use 이름도 와이어에서 정규화(turn 2+ 400 방지).
    assert wire[2]["tool_calls"][0]["function"]["name"] == "retrieval_search"
    # arguments 는 다시 JSON 문자열로 직렬화.
    assert wire[2]["tool_calls"][0]["function"]["arguments"] == '{"q": "a"}'
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"chunks": []}'}


# ── T-3 anthropic ──────────────────────────────────────────────────────────


async def test_anthropic_serializes_tools_system_promotion(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        assert request.url.path.endswith("/messages")
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "검색합니다"},
                    # 와이어 이름(정규화된 `retrieval_scope`)으로 돌려준다 → 복원 검증.
                    {"type": "tool_use", "id": "tu_1", "name": "retrieval_scope", "input": {"q": "z"}},
                ],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        )

    llm = HttpLLM(
        provider="anthropic",
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5",
        api_key="sk-test",
    )
    _patch(monkeypatch, _mock_transport(handler))
    r = await llm.generate_with_tools(
        [ChatMessage(role="system", content="finder 지시"), ChatMessage(role="user", content="질의")],
        tools=[_SCOPE, _VERDICT],
        tool_choice="required",
    )
    # input_schema 매핑 + system 승격 + required→any + 직렬 비활성. 도구 이름은
    # 와이어에서 `.`→`_` 정규화(provider 이름 패턴).
    assert captured["tools"][0] == {
        "name": "retrieval_scope",
        "description": "검색 범위",
        "input_schema": _SCOPE.parameters,
    }
    assert captured["system"] == "finder 지시"
    assert all(m["role"] != "system" for m in captured["messages"])
    assert captured["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    # 파싱: text 누적 + tool_use(input 은 이미 dict) + 이름 복원(registry 점 이름).
    assert r.text == "검색합니다"
    assert r.tool_calls[0].name == "retrieval.scope"
    assert r.tool_calls[0].arguments == {"q": "z"}
    # stop_reason tool_use → tool_calls 정규화.
    assert r.stop_reason == "tool_calls"
    assert r.token_usage == {"prompt_tokens": 7, "completion_tokens": 3}


async def test_anthropic_tool_result_rides_user_turn(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "claude-haiku-4-5", "stop_reason": "end_turn",
                  "content": [{"type": "text", "text": "ok"}],
                  "usage": {"input_tokens": 1, "output_tokens": 1}},
        )

    llm = HttpLLM(provider="anthropic", endpoint="https://api.anthropic.com/v1",
                  model="claude-haiku-4-5", api_key="sk-test")
    _patch(monkeypatch, _mock_transport(handler))
    msgs = [
        ChatMessage(role="user", content="질의"),
        ChatMessage(
            role="assistant", content="",
            tool_calls=(ToolCall("tu_1", "retrieval.search", {"q": "a"}),),
        ),
        ChatMessage(role="tool", content="검색결과", tool_call_id="tu_1", is_error=True),
    ]
    await llm.generate_with_tools(msgs, tools=[_SCOPE])
    wire = captured["messages"]
    # assistant tool_use 블록 — 히스토리 이름도 와이어에서 정규화(turn 2+ 400 방지).
    assert wire[1]["content"][0] == {"type": "tool_use", "id": "tu_1", "name": "retrieval_search", "input": {"q": "a"}}
    # tool_result 는 user 턴 + is_error.
    assert wire[2]["role"] == "user"
    assert wire[2]["content"][0]["type"] == "tool_result"
    assert wire[2]["content"][0]["tool_use_id"] == "tu_1"
    assert wire[2]["content"][0]["is_error"] is True


async def test_anthropic_skips_thinking_block(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "내부 추론"},
                    {"type": "text", "text": "실제 답"},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    llm = HttpLLM(provider="anthropic", endpoint="https://api.anthropic.com/v1",
                  model="claude-opus-4-8", api_key="sk-test")
    _patch(monkeypatch, _mock_transport(handler))
    r = await llm.generate_with_tools([ChatMessage(role="user", content="q")], tools=[_SCOPE])
    assert r.text == "실제 답"  # thinking 제외.


async def test_anthropic_opus_48_omits_sampling_params(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "claude-opus-4-8", "stop_reason": "end_turn",
                  "content": [{"type": "text", "text": "x"}],
                  "usage": {"input_tokens": 1, "output_tokens": 1}},
        )

    llm = HttpLLM(provider="anthropic", endpoint="https://api.anthropic.com/v1",
                  model="claude-opus-4-8", api_key="sk-test")
    _patch(monkeypatch, _mock_transport(handler))
    await llm.generate_with_tools([ChatMessage(role="user", content="q")], tools=[_SCOPE])
    assert "temperature" not in captured  # 4.8 도 샘플링 파라미터 거부(§4.3).


async def test_anthropic_haiku_keeps_temperature(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "claude-haiku-4-5", "stop_reason": "end_turn",
                  "content": [{"type": "text", "text": "x"}],
                  "usage": {"input_tokens": 1, "output_tokens": 1}},
        )

    llm = HttpLLM(provider="anthropic", endpoint="https://api.anthropic.com/v1",
                  model="claude-haiku-4-5", api_key="sk-test")
    _patch(monkeypatch, _mock_transport(handler))
    await llm.generate_with_tools([ChatMessage(role="user", content="q")], tools=[_SCOPE])
    assert captured["temperature"] == 0.0
