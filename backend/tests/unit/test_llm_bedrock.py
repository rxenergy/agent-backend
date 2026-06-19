"""Bedrock provider: same Anthropic Messages wire format, SigV4-signed, targeting
`bedrock-runtime.{region}` with the model id in the URL path and
`anthropic_version` in the body. Credentials come from the botocore chain — these
tests inject static keys via env so `_BedrockSigner` resolves without a live role.
"""
from __future__ import annotations

import base64
import binascii
import json
import struct

import httpx
import pytest

from app.adapters.llm.http import HttpLLM
from app.config.profiles import _build_llm_pool
from app.config.settings import LLMPoolEntry, Settings
from app.ports.llm import ChatMessage, GrammarSpec, ToolSpec


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    """Static AWS creds so botocore resolves a credential without a real role."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _patch_async_client(monkeypatch, transport: httpx.MockTransport):
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr("app.adapters.llm.http.httpx.AsyncClient", factory)


def _event_stream_frame(chunk_event: dict) -> bytes:
    """Encode one Anthropic SSE chunk-event dict as an AWS event-stream frame
    (`:message-type=event`, `:event-type=chunk`, payload `{"bytes": <base64>}`)."""

    def header(name: str, value: str) -> bytes:
        nb, vb = name.encode(), value.encode()
        return bytes([len(nb)]) + nb + bytes([7]) + struct.pack(">H", len(vb)) + vb

    inner = base64.b64encode(json.dumps(chunk_event).encode()).decode()
    payload = json.dumps({"bytes": inner}).encode()
    headers = header(":message-type", "event") + header(":event-type", "chunk")
    total_len = 12 + len(headers) + len(payload) + 4
    prelude = struct.pack(">I", total_len) + struct.pack(">I", len(headers))
    prelude_crc = struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + prelude_crc + headers + payload
    return body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)


# ── factory wiring ────────────────────────────────────────────────────────────


def test_pool_adds_bedrock_entry_region_from_aws_region():
    s = Settings(
        aws_region="us-east-1",
        llm_pool=[
            LLMPoolEntry(
                id="claude-opus-bedrock",
                provider="bedrock",
                model="anthropic.claude-opus-4-8",
            )
        ],
    )
    pool = _build_llm_pool(s)
    llm = pool["claude-opus-bedrock"]
    assert isinstance(llm, HttpLLM)
    assert llm.model_id == "anthropic.claude-opus-4-8"
    # endpoint derived from the profile region
    assert llm._endpoint == "https://bedrock-runtime.us-east-1.amazonaws.com"


def test_pool_bedrock_entry_region_override():
    s = Settings(
        aws_region="us-east-1",
        llm_pool=[
            LLMPoolEntry(
                id="claude-bedrock-apac",
                provider="bedrock",
                model="apac.anthropic.claude-sonnet-4-6",
                region="ap-northeast-2",
            )
        ],
    )
    pool = _build_llm_pool(s)
    assert (
        pool["claude-bedrock-apac"]._endpoint
        == "https://bedrock-runtime.ap-northeast-2.amazonaws.com"
    )


def test_bedrock_requires_region():
    with pytest.raises(ValueError):
        HttpLLM(provider="bedrock", endpoint="", model="anthropic.claude-opus-4-8")


# ── non-streaming generate ──────────────────────────────────────────────────


async def test_bedrock_generate_url_body_and_sigv4(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["host"] = request.url.host
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization", "")
        captured["amz_date"] = request.headers.get("X-Amz-Date", "")
        # no anthropic x-api-key on the bedrock path
        captured["x_api_key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "bedrock hi"}],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
    )
    _patch_async_client(monkeypatch, _mock_transport(handler))
    result = await llm.generate("hi")

    assert result.text == "bedrock hi"
    assert result.token_usage == {"prompt_tokens": 4, "completion_tokens": 2}
    # model id in the URL path (URL-quoted), invoke verb
    assert captured["path"] == "/model/anthropic.claude-opus-4-8/invoke"
    assert captured["host"] == "bedrock-runtime.us-east-1.amazonaws.com"
    # body carries anthropic_version, no `model` field (it's in the URL)
    assert captured["body"]["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in captured["body"]
    # SigV4-signed; no anthropic api-key header
    assert captured["auth"].startswith("AWS4-HMAC-SHA256")
    assert "Credential=AKIAEXAMPLE/" in captured["auth"]
    assert captured["amz_date"]
    assert captured["x_api_key"] is None


async def test_bedrock_arn_model_id_url_quoted(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path.decode()
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}}
        )

    arn = "arn:aws:bedrock:us-east-1:123:inference-profile/us.anthropic.claude-opus-4-8"
    llm = HttpLLM(provider="bedrock", endpoint="", model=arn, region="us-east-1")
    _patch_async_client(monkeypatch, _mock_transport(handler))
    await llm.generate("hi")
    # `:` and `/` in the ARN are percent-encoded into a single path segment
    assert "%3A" in captured["raw_path"]
    assert "%2F" in captured["raw_path"]
    assert "/model/" in captured["raw_path"]
    assert captured["raw_path"].endswith("/invoke")


async def test_bedrock_omits_sampling_params_for_opus_4_8(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}}
        )

    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
    )
    _patch_async_client(monkeypatch, _mock_transport(handler))
    await llm.generate("hi", model_options={"temperature": 0.7})
    assert "temperature" not in captured["body"]


# ── tools ────────────────────────────────────────────────────────────────────


async def test_bedrock_tools_path_and_parse(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "calling"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "retrieval_search",
                        "input": {"q": "smr"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        )

    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-sonnet-4-6",
        region="us-east-1",
    )
    _patch_async_client(monkeypatch, _mock_transport(handler))
    result = await llm.generate_with_tools(
        [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")],
        tools=[
            ToolSpec(
                name="retrieval.search",
                description="search",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ],
    )
    assert captured["path"] == "/model/anthropic.claude-sonnet-4-6/invoke"
    assert captured["body"]["anthropic_version"] == "bedrock-2023-05-31"
    assert captured["body"]["system"] == "sys"
    assert "model" not in captured["body"]
    assert result.stop_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    # dotted registry name restored from the wire name
    assert result.tool_calls[0].name == "retrieval.search"
    assert result.tool_calls[0].arguments == {"q": "smr"}


# ── structured output (json_schema grammar → forced tool, 설계 A) ────────────

_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "necessary_chunk_ids": {"type": "array", "items": {"type": "string"}},
        "multihop_chunk_ids": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["necessary_chunk_ids", "multihop_chunk_ids"],
}


async def test_bedrock_generate_messages_json_schema_forces_tool_and_serializes(monkeypatch):
    # json_schema grammar → 단일 강제 도구로 변환(tool_choice={type:tool}), Claude 의
    # tool_use.input 을 JSON 문자열로 직렬화해 text 로 반환(verify_slot json.loads 호환).
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [
                    # 산문 text 블록은 무시되고, tool_use.input 이 정본이 된다.
                    {"type": "text", "text": "Here is the JSON:"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "structured_output",
                        "input": {"necessary_chunk_ids": ["c1"],
                                  "multihop_chunk_ids": [], "rationale": "c1 only"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )

    llm = HttpLLM(provider="bedrock", endpoint="",
                  model="anthropic.claude-haiku-4-5", region="us-east-1")
    _patch_async_client(monkeypatch, _mock_transport(handler))
    result = await llm.generate_messages(
        [ChatMessage(role="system", content="judge"),
         ChatMessage(role="user", content="chunks...")],
        grammar=GrammarSpec(kind="json_schema", value=_VERIFY_SCHEMA),
    )
    # 강제 도구가 payload 에 실렸다(스키마 = input_schema, tool_choice 강제).
    assert captured["body"]["tools"][0]["name"] == "structured_output"
    assert captured["body"]["tools"][0]["input_schema"] == _VERIFY_SCHEMA
    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "structured_output"}
    # 반환 text 는 tool_use.input 의 JSON 직렬화 → json.loads 로 그대로 파싱된다.
    parsed = json.loads(result.text)
    assert parsed["necessary_chunk_ids"] == ["c1"]
    assert parsed["rationale"] == "c1 only"
    assert result.token_usage == {"prompt_tokens": 12, "completion_tokens": 5}


async def test_bedrock_generate_messages_no_grammar_returns_text(monkeypatch):
    # grammar 없으면 일반 text 응답 그대로(도구 미주입 — 회귀 가드).
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "plain answer"}],
                  "usage": {"input_tokens": 3, "output_tokens": 2}},
        )

    llm = HttpLLM(provider="bedrock", endpoint="",
                  model="anthropic.claude-haiku-4-5", region="us-east-1")
    _patch_async_client(monkeypatch, _mock_transport(handler))
    result = await llm.generate_messages([ChatMessage(role="user", content="hi")])
    assert "tools" not in captured["body"]
    assert "tool_choice" not in captured["body"]
    assert result.text == "plain answer"


async def test_bedrock_generate_messages_json_schema_no_tool_use_falls_back_to_text(monkeypatch):
    # 모델이 강제 도구를 안 내고 text 만 냈을 때(거부 등) — text 폴백(json.loads 는 호출부가
    # 처리). 어댑터는 빈 structured 면 text 블록을 합쳐 돌려준다(silent 손실 방지).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "I cannot comply"}],
                  "usage": {}},
        )

    llm = HttpLLM(provider="bedrock", endpoint="",
                  model="anthropic.claude-haiku-4-5", region="us-east-1")
    _patch_async_client(monkeypatch, _mock_transport(handler))
    result = await llm.generate_messages(
        [ChatMessage(role="user", content="x")],
        grammar=GrammarSpec(kind="json_schema", value=_VERIFY_SCHEMA),
    )
    assert result.text == "I cannot comply"


# ── streaming (AWS event-stream framing) ─────────────────────────────────────


async def test_bedrock_stream_decodes_event_stream(monkeypatch):
    frames = b"".join(
        _event_stream_frame(ev)
        for ev in [
            {
                "type": "message_start",
                "message": {
                    "model": "anthropic.claude-opus-4-8",
                    "usage": {"input_tokens": 7},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hel"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "lo"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 9},
            },
            {"type": "message_stop"},
        ]
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, content=frames)

    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
    )
    _patch_async_client(monkeypatch, _mock_transport(handler))

    contents: list[str] = []
    finish: str | None = None
    usage: dict = {}
    async for delta in llm.generate_stream("hi"):
        if delta.content:
            contents.append(delta.content)
        if delta.finish_reason:
            finish = delta.finish_reason
            usage = delta.token_usage

    # streaming verb + no `stream:true` body field (Bedrock rejects it)
    assert captured["path"].endswith("/invoke-with-response-stream")
    assert "stream" not in captured["body"]
    assert captured["auth"].startswith("AWS4-HMAC-SHA256")
    assert "".join(contents) == "hello"
    assert finish == "stop"  # end_turn → stop
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 9


async def test_bedrock_stream_json_schema_forces_tool_and_streams_input(monkeypatch):
    """스트리밍 경로에서 json_schema grammar → 단일 강제 도구(tool_choice). Claude 가
    스키마 준수 input 을 `input_json_delta.partial_json` 으로 흘리면 그 partial JSON 을
    content 로 누적 forward 한다(호출부 버퍼가 합쳐 유효 JSON). 강제 도구가 걸리면
    thinking 은 비활성(payload 에 thinking 없음) — 비스트리밍 forced-tool 과 동형."""
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    frames = b"".join(
        _event_stream_frame(ev)
        for ev in [
            {"type": "message_start",
             "message": {"model": "anthropic.claude-opus-4-8",
                         "usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "name": "structured_output"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"verdict":'}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": ' "supported"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 6}},
            {"type": "message_stop"},
        ]
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=frames)

    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
    )
    _patch_async_client(monkeypatch, _mock_transport(handler))

    contents: list[str] = []
    async for delta in llm.generate_stream(
        "hi", grammar=GrammarSpec(kind="json_schema", value=schema)
    ):
        if delta.content:
            contents.append(delta.content)

    # 강제 도구가 payload 에 실렸고(스키마=input_schema, tool_choice 강제), thinking 은 꺼짐.
    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "structured_output"}
    assert captured["body"]["tools"][0]["input_schema"] == schema
    assert "thinking" not in captured["body"]
    # partial_json 조각이 합쳐져 유효 JSON 이 된다(_parse / json.loads 호환).
    assert json.loads("".join(contents)) == {"verdict": "supported"}


async def test_bedrock_stream_exception_frame_raises(monkeypatch):
    from app.adapters.llm.http import LLMUnavailableError

    def exception_frame() -> bytes:
        def header(name: str, value: str) -> bytes:
            nb, vb = name.encode(), value.encode()
            return bytes([len(nb)]) + nb + bytes([7]) + struct.pack(">H", len(vb)) + vb

        payload = json.dumps({"message": "model stream error"}).encode()
        headers = (
            header(":message-type", "exception")
            + header(":exception-type", "modelStreamErrorException")
        )
        total_len = 12 + len(headers) + len(payload) + 4
        prelude = struct.pack(">I", total_len) + struct.pack(">I", len(headers))
        prelude_crc = struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
        body = prelude + prelude_crc + headers + payload
        return body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=exception_frame())

    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
        max_attempts=1,
    )
    _patch_async_client(monkeypatch, _mock_transport(handler))
    with pytest.raises(LLMUnavailableError):
        async for _ in llm.generate_stream("hi"):
            pass


# ── bearer-token auth (short-term Bedrock API key, no SigV4 / no IAM) ─────────


def _clear_aws_creds(monkeypatch):
    """Prove the bearer-token path needs no AWS credentials at all."""
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
    ):
        monkeypatch.delenv(var, raising=False)


def test_bedrock_bearer_token_skips_sigv4_signer(monkeypatch):
    _clear_aws_creds(monkeypatch)
    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
        api_key="bedrock-api-key-abc123",
    )
    # No SigV4 signer is constructed when a bearer token is present — so no AWS
    # credential resolution happens (this would raise otherwise with creds cleared).
    assert llm._signer is None
    assert llm._bedrock_bearer_token == "bedrock-api-key-abc123"


async def test_bedrock_bearer_token_explicit_authorization_header(monkeypatch):
    _clear_aws_creds(monkeypatch)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        captured["amz_date"] = request.headers.get("X-Amz-Date")
        captured["body"] = json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}], "usage": {}},
        )

    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
        api_key="bedrock-api-key-abc123",
    )
    _patch_async_client(monkeypatch, _mock_transport(handler))
    await llm.generate("hi")

    assert captured["auth"] == "Bearer bedrock-api-key-abc123"
    # not SigV4-signed
    assert captured["amz_date"] is None
    # same Bedrock URL + body shape as the SigV4 path
    assert captured["path"] == "/model/anthropic.claude-opus-4-8/invoke"
    assert captured["body"]["anthropic_version"] == "bedrock-2023-05-31"


async def test_bedrock_bearer_token_from_env_fallback(monkeypatch):
    _clear_aws_creds(monkeypatch)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-api-key-fromenv")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}}
        )

    # No api_key passed — falls back to AWS_BEARER_TOKEN_BEDROCK.
    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
    )
    assert llm._signer is None
    _patch_async_client(monkeypatch, _mock_transport(handler))
    await llm.generate("hi")
    assert captured["auth"] == "Bearer bedrock-api-key-fromenv"


async def test_bedrock_bearer_token_streaming(monkeypatch):
    _clear_aws_creds(monkeypatch)
    frames = b"".join(
        _event_stream_frame(ev)
        for ev in [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hi"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
            {"type": "message_stop"},
        ]
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        captured["amz_date"] = request.headers.get("X-Amz-Date")
        return httpx.Response(200, content=frames)

    llm = HttpLLM(
        provider="bedrock",
        endpoint="",
        model="anthropic.claude-opus-4-8",
        region="us-east-1",
        api_key="bedrock-api-key-abc123",
    )
    _patch_async_client(monkeypatch, _mock_transport(handler))
    out = [d.content async for d in llm.generate_stream("hi") if d.content]
    assert "".join(out) == "hi"
    assert captured["auth"] == "Bearer bedrock-api-key-abc123"
    assert captured["amz_date"] is None


def test_pool_bedrock_bearer_token_via_api_key_env(monkeypatch):
    _clear_aws_creds(monkeypatch)
    monkeypatch.setenv("MY_BEDROCK_TOKEN", "bedrock-api-key-pool")
    s = Settings(
        aws_region="us-east-1",
        llm_pool=[
            LLMPoolEntry(
                id="claude-bedrock-key",
                provider="bedrock",
                model="anthropic.claude-opus-4-8",
                api_key_env="MY_BEDROCK_TOKEN",
            )
        ],
    )
    pool = _build_llm_pool(s)
    llm = pool["claude-bedrock-key"]
    assert isinstance(llm, HttpLLM)
    assert llm._signer is None
    assert llm._bedrock_bearer_token == "bedrock-api-key-pool"
