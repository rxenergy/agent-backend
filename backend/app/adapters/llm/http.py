from __future__ import annotations

import json
import os
import re
from typing import Any, AsyncIterator, Literal

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.ports.llm import (
    ChatMessage,
    GrammarSpec,
    LLMPort,
    LLMResult,
    LLMToolResult,
    LLMTokenDelta,
    LLMUnavailableError,
    ToolCall,
    ToolChoice,
    ToolSpec,
)

__all__ = ["HttpLLM", "LLMUnavailableError"]

Provider = Literal["openai_compat", "anthropic", "bedrock"]

# Bedrock runtime carries the Anthropic version in the request *body* (not a
# header) under this fixed token; the request is either SigV4-signed (AWS creds)
# or sent with an `Authorization: Bearer` short-term Bedrock API key.
_BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"
_BEDROCK_SERVICE = "bedrock"
# Env var each Anthropic/AWS SDK auto-detects for the Bedrock short-term API key
# (a `bedrock-api-key-...` bearer token). Used as the fallback when a bedrock pool
# entry sets no explicit api_key_env.
_BEDROCK_BEARER_TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"
# Anthropic/Bedrock 구조화 출력용 강제 도구 이름 — json_schema grammar 를 단일 도구의
# input_schema 로 주입해 Claude 가 스키마 준수 JSON(tool_use.input)을 내게 한다. vLLM
# guided_json 의 Anthropic/Bedrock 대응물(generate_messages 비-openai_compat 경로).
_STRUCTURED_TOOL_NAME = "structured_output"


class HttpLLM(LLMPort):
    """OpenAI /v1/chat/completions compatible client (vLLM, OpenAI, LM Studio, Ollama)
    plus Anthropic /v1/messages and Amazon Bedrock. Single adapter, provider-switched
    at construction time.

    `provider="bedrock"` reuses the entire Anthropic Messages wire format (system
    promotion, tools, streaming SSE) but targets `bedrock-runtime.{region}`. The
    `model` is a Bedrock model id / inference-profile id placed in the URL path,
    and `anthropic_version` moves into the body. Two auth modes:

    - **Bearer token** (short-term Bedrock API key): if `api_key` is set or the
      `AWS_BEARER_TOKEN_BEDROCK` env var is present, the request carries
      `Authorization: Bearer <token>` and is **not** SigV4-signed — no AWS
      access key/secret or IAM role needed.
    - **SigV4** (fallback): no bearer token → sign with the standard AWS
      credential chain (env keys, shared profile, or IAM role) via botocore.
    """

    def __init__(
        self,
        *,
        provider: Provider,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        max_attempts: int = 2,
        anthropic_version: str = "2023-06-01",
        region: str | None = None,
    ) -> None:
        # bedrock 은 region 에서 endpoint 를 유도하므로 endpoint 가 비어도 된다.
        if provider == "bedrock":
            if not region:
                raise ValueError("HttpLLM bedrock provider requires a region")
            self._region = region
            self._endpoint = (
                endpoint.rstrip("/")
                or f"https://bedrock-runtime.{region}.amazonaws.com"
            )
            # 베어러 토큰(임시 API 키) 우선: pool entry 의 api_key_env → 없으면
            # AWS_BEARER_TOKEN_BEDROCK env. 토큰이 있으면 SigV4 서명을 건너뛴다.
            self._bedrock_bearer_token = api_key or os.getenv(_BEDROCK_BEARER_TOKEN_ENV)
            self._signer = (
                None if self._bedrock_bearer_token else _BedrockSigner(region)
            )
        else:
            if not endpoint:
                raise ValueError("HttpLLM requires a non-empty endpoint")
            self._region = region or ""
            self._endpoint = endpoint.rstrip("/")
            self._bedrock_bearer_token = None
            self._signer = None
        self._provider: Provider = provider
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._anthropic_version = anthropic_version

    # ── Anthropic/Bedrock wire helpers ───────────────────────────────────────
    # The Anthropic-family call sites differ only in URL, headers, body-version
    # field, and whether the request is SigV4-signed. Centralize that here so the
    # four methods (generate / messages / tools / stream) stay provider-agnostic.

    def _anthropic_url(self, *, stream: bool = False) -> str:
        if self._provider == "bedrock":
            verb = "invoke-with-response-stream" if stream else "invoke"
            # model id may contain `/`, `:` (inference-profile ARN) — URL-quote it.
            from urllib.parse import quote

            return f"{self._endpoint}/model/{quote(self._model, safe='')}/{verb}"
        return f"{self._endpoint}/messages"

    def _anthropic_base_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._provider == "bedrock":
            # version-in-body; no anthropic-version header. Bearer-token auth sets
            # Authorization here; SigV4 mode adds its headers later in _post_anthropic.
            headers["Accept"] = "application/json"
            if self._bedrock_bearer_token:
                headers["Authorization"] = f"Bearer {self._bedrock_bearer_token}"
            return headers
        headers["anthropic-version"] = self._anthropic_version
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _finalize_anthropic_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bedrock invoke takes the model id in the URL, not the body, and needs
        `anthropic_version` in the body. Mutates/returns a body shaped per provider."""
        if self._provider == "bedrock":
            payload = dict(payload)
            payload.pop("model", None)
            payload["anthropic_version"] = _BEDROCK_ANTHROPIC_VERSION
        return payload

    async def _post_anthropic(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        """Single non-streaming POST, SigV4-signed when provider=bedrock."""
        body = json.dumps(payload).encode("utf-8")
        if self._signer is not None:
            headers = {**headers, **self._signer.sign(url, body)}
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            return await client.post(url, content=body, headers=headers)

    @property
    def model_id(self) -> str:
        return self._model

    async def generate(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> LLMResult:
        opts = dict(model_options or {})
        max_tokens = int(opts.pop("max_tokens", 1024))
        temperature = float(opts.pop("temperature", 0.0))
        extra = _sampling_extras(opts)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.RemoteProtocolError, _Retry5xx)
                ),
                reraise=True,
            ):
                with attempt:
                    if self._provider == "openai_compat":
                        return await self._call_openai_compat(
                            prompt, max_tokens, temperature, grammar, extra
                        )
                    return await self._call_anthropic(prompt, max_tokens, temperature)
        except (httpx.HTTPError, RetryError, _Retry5xx) as exc:
            raise LLMUnavailableError(str(exc)) from exc

        raise LLMUnavailableError("HttpLLM: retry loop exited without result")

    async def generate_stream(
        self,
        prompt: str,
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> AsyncIterator[LLMTokenDelta]:
        """Stream tokens for OpenAI-compatible and Anthropic providers.

        Anthropic SSE differs from OpenAI: events use `event:` + `data:` line
        pairs (no `[DONE]` sentinel), thinking is its own content block
        emitting `thinking_delta`, and `message_delta.usage.output_tokens` is
        cumulative-final. Extended thinking is auto-enabled (adaptive +
        summarized display) on models that support it so the runner can
        surface reasoning as OpenAI-compat `reasoning_content` deltas.
        """
        opts = dict(model_options or {})
        max_tokens = int(opts.pop("max_tokens", 1024))
        temperature = float(opts.pop("temperature", 0.0))
        extra = _sampling_extras(opts)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.RemoteProtocolError, _Retry5xx)
                ),
                reraise=True,
            ):
                with attempt:
                    if self._provider == "openai_compat":
                        async for delta in self._stream_openai_compat(
                            prompt, max_tokens, temperature, grammar, extra
                        ):
                            yield delta
                    else:
                        async for delta in self._stream_anthropic(
                            prompt, max_tokens, temperature, grammar
                        ):
                            yield delta
                    return
        except (httpx.HTTPError, RetryError, _Retry5xx) as exc:
            raise LLMUnavailableError(str(exc)) from exc

    async def generate_messages(
        self,
        messages: list[ChatMessage],
        *,
        model_options: dict[str, Any] | None = None,
        grammar: GrammarSpec | None = None,
    ) -> LLMResult:
        """Non-streaming, 도구 없는 멀티메시지 생성 1회(structured output 지원).
        system+user(+이력) 메시지에 guided decoding(grammar)만 거는 호출자(참조
        추출 등)를 위한 경로다. 메시지 직렬화는 `generate_with_tools` 와 동일한
        `_openai_message` 를, grammar 적용은 `generate`/`generate_stream` 와 동일한
        `_apply_grammar_to_openai_payload` 를 재사용한다(원칙 #4)."""
        opts = dict(model_options or {})
        max_tokens = int(opts.pop("max_tokens", 1024))
        temperature = float(opts.pop("temperature", 0.0))
        extra = _sampling_extras(opts)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.RemoteProtocolError, _Retry5xx)
                ),
                reraise=True,
            ):
                with attempt:
                    if self._provider == "openai_compat":
                        return await self._call_openai_compat_messages(
                            messages, max_tokens, temperature, grammar, extra
                        )
                    return await self._call_anthropic_messages(
                        messages, max_tokens, temperature, grammar
                    )
        except (httpx.HTTPError, RetryError, _Retry5xx) as exc:
            raise LLMUnavailableError(str(exc)) from exc

        raise LLMUnavailableError("HttpLLM: messages retry loop exited without result")

    async def generate_with_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        tool_choice: ToolChoice = "auto",
        model_options: dict[str, Any] | None = None,
        parallel_tool_calls: bool = False,
    ) -> LLMToolResult:
        """Non-streaming 도구 호출 1턴(설계 §3–4). 멀티턴 agentic 루프는 호출자
        (Finder, application 계층)가 소유하고, 이 어댑터는 "messages+tools →
        (text, tool_calls, stop_reason)" 1회 변환만 책임진다(원칙 #1/§2). 중립
        타입↔provider 와이어 포맷 변환은 여기 어댑터 안에 가둔다(원칙 #4)."""
        opts = dict(model_options or {})
        max_tokens = int(opts.pop("max_tokens", 1024))
        temperature = float(opts.pop("temperature", 0.0))

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.RemoteProtocolError, _Retry5xx)
                ),
                reraise=True,
            ):
                with attempt:
                    if self._provider == "openai_compat":
                        return await self._call_openai_compat_tools(
                            messages, tools, tool_choice, max_tokens,
                            temperature, parallel_tool_calls,
                        )
                    return await self._call_anthropic_tools(
                        messages, tools, tool_choice, max_tokens,
                        temperature, parallel_tool_calls,
                    )
        except (httpx.HTTPError, RetryError, _Retry5xx) as exc:
            raise LLMUnavailableError(str(exc)) from exc

        raise LLMUnavailableError("HttpLLM: tool-call retry loop exited without result")

    async def _call_openai_compat_tools(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        tool_choice: ToolChoice,
        max_tokens: int,
        temperature: float,
        parallel_tool_calls: bool,
    ) -> LLMToolResult:
        url = f"{self._endpoint}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_openai_message(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": [_openai_tool(t) for t in tools],
            "tool_choice": _openai_tool_choice(tool_choice),
            # 직렬 기본 — 워크플로우가 1턴 1도구를 제어하므로 instrumentation·순서
            # 보장을 단순화한다(설계 §3). vLLM/OpenAI 둘 다 top-level 필드.
            "parallel_tool_calls": parallel_tool_calls,
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
        _raise_for_status(resp)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        restore = _restore_map(tools)
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            wire_name = str(fn.get("name") or "")
            tool_calls.append(
                ToolCall(
                    id=str(tc.get("id") or ""),
                    name=restore.get(wire_name, wire_name),
                    arguments=_parse_json_args(raw_args),
                )
            )
        usage = data.get("usage") or {}
        return LLMToolResult(
            text=text,
            tool_calls=tuple(tool_calls),
            stop_reason=str(choice.get("finish_reason") or "stop"),
            token_usage={
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
            },
            model_id=str(data.get("model") or self._model),
        )

    async def _call_anthropic_tools(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        tool_choice: ToolChoice,
        max_tokens: int,
        temperature: float,
        parallel_tool_calls: bool,
    ) -> LLMToolResult:
        url = self._anthropic_url()
        headers = self._anthropic_base_headers()

        # system 메시지는 messages 배열이 아니라 top-level `system` 필드로 승격한다.
        system_text = "\n\n".join(
            m.content for m in messages if m.role == "system" and m.content
        )
        wire_messages = [
            _anthropic_message(m) for m in messages if m.role != "system"
        ]
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": wire_messages,
            "tools": [_anthropic_tool(t) for t in tools],
            "tool_choice": _anthropic_tool_choice(tool_choice, parallel_tool_calls),
        }
        if system_text:
            payload["system"] = system_text
        # Opus 4.7/4.8 은 temperature/top_p/top_k 를 400 으로 거부한다(§4.3) —
        # 도구 호출 경로에서 두 모델 모두 샘플링 파라미터를 전송하지 않는다.
        if not _rejects_sampling_params(self._model):
            payload["temperature"] = temperature

        resp = await self._post_anthropic(
            url, self._finalize_anthropic_payload(payload), headers
        )
        _raise_for_status(resp)
        data = resp.json()
        restore = _restore_map(tools)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                wire_name = str(block.get("name") or "")
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or ""),
                        name=restore.get(wire_name, wire_name),
                        arguments=dict(block.get("input") or {}),
                    )
                )
            # `thinking` 블록은 text 에 포함하지 않고 건너뛴다(§4.2).
        usage = data.get("usage") or {}
        return LLMToolResult(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            stop_reason=_map_anthropic_stop_reason(data.get("stop_reason")),
            token_usage={
                "prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
            },
            model_id=str(data.get("model") or self._model),
        )

    async def _call_openai_compat(
        self, prompt: str, max_tokens: int, temperature: float,
        grammar: GrammarSpec | None = None,
        extra: dict[str, Any] | None = None,
    ) -> LLMResult:
        return await self._call_openai_compat_messages(
            [ChatMessage(role="user", content=prompt)],
            max_tokens, temperature, grammar, extra,
        )

    async def _call_openai_compat_messages(
        self, messages: list[ChatMessage], max_tokens: int, temperature: float,
        grammar: GrammarSpec | None = None,
        extra: dict[str, Any] | None = None,
    ) -> LLMResult:
        url = f"{self._endpoint}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_openai_message(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            **(extra or {}),
        }
        _apply_grammar_to_openai_payload(payload, grammar)
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
        _raise_for_status(resp)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or choice.get("text") or ""
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            token_usage={
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
            },
            model_id=str(data.get("model") or self._model),
        )

    async def _call_anthropic_messages(
        self, messages: list[ChatMessage], max_tokens: int, temperature: float,
        grammar: GrammarSpec | None = None,
    ) -> LLMResult:
        """도구 없는 멀티메시지 경로의 Anthropic/Bedrock 변형. system 은 top-level 필드로
        승격한다(`_call_anthropic_tools` 와 동형).

        **구조화 출력(json_schema grammar):** Anthropic/Bedrock 은 vLLM `guided_json` 이
        없으므로, json_schema grammar 를 **단일 강제 도구**(`tool_choice={type:"tool"}`)로
        변환해 그 도구의 `input_schema` 로 스키마를 강제한다 — Claude 가 그 스키마에 맞는
        `tool_use.input` 을 내고, 어댑터가 그 input 을 JSON 문자열로 직렬화해 `text` 로
        돌려준다(호출부 `json.loads` 가 그대로 파싱). 이로써 verify_slot/ref_extract 등
        guided decoding 의존 호출이 Bedrock 에서도 유효 JSON 을 받는다(설계 A — bedrock
        구조화 출력). grammar 가 없거나 json_schema 가 아니면 일반 text 응답 그대로."""
        url = self._anthropic_url()
        headers = self._anthropic_base_headers()
        system_text = "\n\n".join(
            m.content for m in messages if m.role == "system" and m.content
        )
        wire_messages = [
            _anthropic_message(m) for m in messages if m.role != "system"
        ]
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": wire_messages,
        }
        if system_text:
            payload["system"] = system_text
        if not _rejects_sampling_params(self._model):
            payload["temperature"] = temperature
        # json_schema → 단일 강제 도구로 스키마 주입(Bedrock/Anthropic 구조화 출력).
        forced_json = grammar is not None and grammar.kind == "json_schema" \
            and isinstance(grammar.value, dict)
        if forced_json:
            payload["tools"] = [{
                "name": _STRUCTURED_TOOL_NAME,
                "description": "Return the answer strictly as this JSON object.",
                "input_schema": grammar.value,
            }]
            payload["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL_NAME}

        resp = await self._post_anthropic(
            url, self._finalize_anthropic_payload(payload), headers
        )
        _raise_for_status(resp)
        data = resp.json()
        text_parts: list[str] = []
        structured: dict[str, Any] | None = None
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use" and block.get("name") == _STRUCTURED_TOOL_NAME:
                # 강제 도구의 input 이 스키마 준수 JSON 객체 — 이것을 정본으로 쓴다.
                inp = block.get("input")
                if isinstance(inp, dict):
                    structured = inp
        usage = data.get("usage") or {}
        # 강제 JSON 이면 tool_use.input 을 직렬화해 text 로(호출부 json.loads 호환). 산문
        # text 블록은 무시한다(스키마 정본이 input). tool_use 가 없으면(거부 등) text 폴백.
        text = json.dumps(structured, ensure_ascii=False) if structured is not None \
            else "".join(text_parts)
        return LLMResult(
            text=text,
            token_usage={
                "prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
            },
            model_id=str(data.get("model") or self._model),
        )

    async def _stream_openai_compat(
        self, prompt: str, max_tokens: int, temperature: float,
        grammar: GrammarSpec | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMTokenDelta]:
        url = f"{self._endpoint}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            # vLLM + OpenAI both honor this — surface usage in the final
            # chunk so the runner can populate token_usage even when
            # streaming.
            "stream_options": {"include_usage": True},
            **(extra or {}),
        }
        _apply_grammar_to_openai_payload(payload, grammar)

        finish_reason: str | None = None
        model_id_seen: str | None = None
        usage_seen: dict[str, int] = {}

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if 500 <= resp.status_code < 600:
                    body = await resp.aread()
                    raise _Retry5xx(f"upstream {resp.status_code}: {body[:256]!r}")
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise LLMUnavailableError(
                        f"upstream {resp.status_code}: {body[:256]!r}"
                    )

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        # SSE comment / keepalive.
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("model"):
                        model_id_seen = str(chunk["model"])
                    usage = chunk.get("usage") or {}
                    if usage:
                        usage_seen = {
                            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                            "completion_tokens": int(usage.get("completion_tokens", 0)),
                        }

                    choices = chunk.get("choices") or []
                    if not choices:
                        # Some providers emit a usage-only terminal chunk
                        # with empty choices — already captured above.
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    reasoning = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    finish_reason = choice.get("finish_reason") or finish_reason
                    if content or reasoning:
                        yield LLMTokenDelta(
                            content=content,
                            reasoning=reasoning,
                        )

        yield LLMTokenDelta(
            finish_reason=finish_reason or "stop",
            token_usage=usage_seen,
            model_id=model_id_seen or self._model,
        )

    async def _stream_anthropic(
        self, prompt: str, max_tokens: int, temperature: float,
        grammar: GrammarSpec | None = None,
    ) -> AsyncIterator[LLMTokenDelta]:
        url = self._anthropic_url(stream=True)
        headers = self._anthropic_base_headers()

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Bedrock streams over the AWS event-stream protocol and signals
        # streaming via the `/invoke-with-response-stream` path, not a `stream`
        # body field — sending `stream:true` to Bedrock is rejected.
        if self._provider != "bedrock":
            payload["stream"] = True
        # Opus 4.7/4.8 reject temperature/top_p/top_k with 400; suppress them
        # there. For other models pass the configured value.
        if not _rejects_sampling_params(self._model):
            payload["temperature"] = temperature
        # 구조화 출력(json_schema grammar): vLLM `guided_json` 이 없는 Anthropic/Bedrock
        # 에선 스키마를 **단일 강제 도구**(tool_choice={type:"tool"})로 강제한다
        # (`_call_anthropic` 비스트리밍과 동형). Claude 가 스키마 준수 `tool_use.input` 을
        # 스트리밍 `input_json_delta` 로 흘리면 그 partial JSON 을 content 로 forward 한다.
        # 강제 도구가 걸리면 thinking 은 비활성되므로(Anthropic 제약), reasoning 표시는
        # 호출부의 구조화 `reasoning` 필드 backstop(extract_reasoning)이 담당한다.
        forced_json = grammar is not None and grammar.kind == "json_schema" \
            and isinstance(grammar.value, dict)
        if forced_json:
            payload["tools"] = [{
                "name": _STRUCTURED_TOOL_NAME,
                "description": "Return the answer strictly as this JSON object.",
                "input_schema": grammar.value,
            }]
            payload["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL_NAME}
        # Auto-enable adaptive thinking with visible summary on models that
        # support it. The runner forwards `thinking_delta` text into the
        # OpenAI-compat `delta.reasoning_content` field so OpenWebUI shows
        # it in its reasoning pane. 강제 도구와 thinking 은 양립 불가라 forced_json
        # 이면 thinking 을 끈다(400 회피).
        elif _supports_adaptive_thinking(self._model):
            payload["thinking"] = {
                "type": "adaptive",
                "display": "summarized",
            }
        payload = self._finalize_anthropic_payload(payload)
        body = json.dumps(payload).encode("utf-8")
        if self._signer is not None:
            headers = {**headers, **self._signer.sign(url, body)}

        # Track per-content-block type so deltas route correctly.
        # Anthropic sends content_block_start with type="thinking" or "text"
        # (and "tool_use" for tools); deltas carry their own `delta.type`
        # but we still need the block type to know whether `thinking_delta`
        # in this block should be surfaced.
        block_types: dict[int, str] = {}
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_create = 0
        finish_reason: str | None = None
        model_id_seen: str | None = None

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            async with client.stream(
                "POST", url, content=body, headers=headers
            ) as resp:
                if 500 <= resp.status_code < 600:
                    err_body = await resp.aread()
                    raise _Retry5xx(f"upstream {resp.status_code}: {err_body[:256]!r}")
                if resp.status_code >= 400:
                    err_body = await resp.aread()
                    raise LLMUnavailableError(
                        f"upstream {resp.status_code}: {err_body[:256]!r}"
                    )

                # Bedrock wraps each Anthropic SSE chunk in an AWS event-stream
                # binary frame; first-party Anthropic emits plain `data:` SSE.
                # Both decode to the same chunk-event dicts, routed identically.
                if self._provider == "bedrock":
                    events = _iter_bedrock_event_stream(resp)
                else:
                    events = _iter_anthropic_sse(resp)

                async for evt in events:
                    etype = evt.get("type")

                    if etype == "message_start":
                        msg = evt.get("message") or {}
                        if msg.get("model"):
                            model_id_seen = str(msg["model"])
                        usage = msg.get("usage") or {}
                        input_tokens = int(usage.get("input_tokens", 0))
                        cache_read = int(usage.get("cache_read_input_tokens", 0))
                        cache_create = int(usage.get("cache_creation_input_tokens", 0))
                    elif etype == "content_block_start":
                        idx = int(evt.get("index", 0))
                        cb = evt.get("content_block") or {}
                        block_types[idx] = str(cb.get("type") or "")
                    elif etype == "content_block_delta":
                        delta = evt.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text = delta.get("text") or ""
                            if text:
                                yield LLMTokenDelta(content=text)
                        elif dtype == "thinking_delta":
                            text = delta.get("thinking") or ""
                            if text:
                                yield LLMTokenDelta(reasoning=text)
                        elif dtype == "input_json_delta" and forced_json:
                            # 강제 도구의 input(스키마 준수 JSON)이 partial JSON 문자열로
                            # 도착 — content 로 누적 forward 한다. 호출부 버퍼가 합치면
                            # 완결 JSON 이 되어 `json.loads`(_parse) 가 그대로 파싱한다.
                            partial = delta.get("partial_json") or ""
                            if partial:
                                yield LLMTokenDelta(content=partial)
                        # signature_delta intentionally not forwarded — it is an
                        # opaque attestation token (only matters when replaying
                        # thinking blocks in a follow-up turn, which the runner
                        # doesn't do). Non-forced tool_use streaming isn't wired.
                    elif etype == "content_block_stop":
                        block_types.pop(int(evt.get("index", -1)), None)
                    elif etype == "message_delta":
                        d = evt.get("delta") or {}
                        if d.get("stop_reason"):
                            finish_reason = str(d["stop_reason"])
                        usage = evt.get("usage") or {}
                        # output_tokens here is cumulative-final — overwrite.
                        if "output_tokens" in usage:
                            output_tokens = int(usage["output_tokens"])
                    elif etype == "message_stop":
                        break
                    elif etype == "error":
                        err = evt.get("error") or {}
                        raise LLMUnavailableError(
                            f"anthropic stream error: {err.get('type')}: "
                            f"{err.get('message')}"
                        )
                    # `ping` and unknown event types are ignored.

        # Map Anthropic stop_reason → OpenAI-compat finish_reason vocabulary
        # so downstream (smr_agent + OpenAI chunk frame) stays consistent.
        finish_mapped = _map_anthropic_stop_reason(finish_reason)
        yield LLMTokenDelta(
            finish_reason=finish_mapped,
            token_usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
            model_id=model_id_seen or self._model,
        )

    async def _call_anthropic(
        self, prompt: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        url = self._anthropic_url()
        headers = self._anthropic_base_headers()
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if not _rejects_sampling_params(self._model):
            payload["temperature"] = temperature
        resp = await self._post_anthropic(
            url, self._finalize_anthropic_payload(payload), headers
        )
        _raise_for_status(resp)
        data = resp.json()
        parts = data.get("content") or []
        text = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            token_usage={
                "prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
            },
            model_id=str(data.get("model") or self._model),
        )


_ADAPTIVE_THINKING_MODELS = ("claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")


def _canonical_model_id(model_id: str) -> str:
    """Strip Bedrock provider/region prefixes so capability checks match the
    same `claude-*` family names on both first-party and Bedrock model ids.

    Bedrock ids carry an `anthropic.` prefix (`anthropic.claude-opus-4-8`) and
    inference-profile ids add a region prefix (`apac.anthropic.claude-…`,
    `us.anthropic.claude-…`). The capability table keys off the bare
    `claude-…` name, so drop everything up to and including `anthropic.`."""
    marker = "anthropic."
    idx = model_id.rfind(marker)
    return model_id[idx + len(marker):] if idx != -1 else model_id


def _supports_adaptive_thinking(model_id: str) -> bool:
    """Adaptive thinking is GA on Opus 4.6/4.7 and Sonnet 4.6. Haiku and
    older snapshots either don't support it or use the legacy `enabled +
    budget_tokens` form, so we don't auto-attach. Match on prefix to absorb
    date-suffix snapshots (e.g. `claude-opus-4-7-20260301`)."""
    canonical = _canonical_model_id(model_id)
    return any(canonical.startswith(m) for m in _ADAPTIVE_THINKING_MODELS)


def _is_opus_4_7(model_id: str) -> bool:
    """Opus 4.7 returns 400 if `temperature` / `top_p` / `top_k` are present —
    the sampling parameters were removed there. Match on prefix to absorb
    future date-suffix snapshots."""
    return _canonical_model_id(model_id).startswith("claude-opus-4-7")


def _rejects_sampling_params(model_id: str) -> bool:
    """Opus 4.7 *and* 4.8 reject temperature/top_p/top_k with 400 (§4.3). The
    tool-calling path generalizes the 4.7-only `_is_opus_4_7` guard to both so
    the same call works against either snapshot. Match on prefix to absorb
    date-suffix snapshots (e.g. `claude-opus-4-8-20260301`). vLLM unaffected."""
    return _canonical_model_id(model_id).startswith(
        ("claude-opus-4-7", "claude-opus-4-8")
    )


# ── 도구 호출 직렬화/파싱(중립 ↔ provider 와이어, 설계 §4) ──────────────────

# Anthropic/OpenAI 둘 다 도구 이름이 `^[a-zA-Z0-9_-]{1,128}$` 를 만족해야 한다(미충족
# 시 400). 우리 registry 이름은 점 네임스페이스(예: `retrieval.search`)라 와이어에서만
# `.` → `_` 로 치환해 보내고(아래 _wire_tool_name), 응답의 tool_call 이름은 요청에 실은
# `tools` 목록으로 만든 역매핑으로 원래 점 이름으로 복원한다(_restore_map). 중립 타입은
# 점 이름을 유지(원칙 #4) — ToolExecutor 의 registry 호출/이름 매칭은 불변.
_DISALLOWED_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]")


def _wire_tool_name(name: str) -> str:
    """registry 도구 이름 → provider 와이어 이름. 허용 패턴 밖 문자(주로 `.`)를 `_` 로
    치환. 유효 이름은 불변(idempotent)."""
    return _DISALLOWED_TOOL_NAME.sub("_", name)


def _restore_map(tools: list[ToolSpec]) -> dict[str, str]:
    """와이어 이름 → 원래(점) 이름 역매핑. 요청에 실은 tools 로 만들어 복원이 정확하다
    (맹목 `_`→`.` 역치환 금지 — submit_verdict 같은 이름을 망가뜨린다)."""
    return {_wire_tool_name(t.name): t.name for t in tools}


def _parse_json_args(raw: Any) -> dict[str, Any]:
    """OpenAI `function.arguments` 는 JSON *문자열* 이므로 dict 로 파싱한다.
    Anthropic `input` 처럼 이미 dict 면 그대로 통과. 파싱 실패/비-object 는 빈
    dict(이후 ToolExecutor 가 registry 스키마로 검증해 error_code 로 귀결, §9)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _openai_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _wire_tool_name(tool.name),
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _openai_tool_choice(choice: ToolChoice) -> Any:
    if choice.startswith("tool:"):
        return {"type": "function",
                "function": {"name": _wire_tool_name(choice[len("tool:"):])}}
    # "auto" | "required" | "none" 은 그대로 전달.
    return choice


def _openai_message(msg: ChatMessage) -> dict[str, Any]:
    if msg.role == "assistant":
        out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": _wire_tool_name(tc.name),
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in msg.tool_calls
            ]
        return out
    if msg.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id or "",
            "content": msg.content or "",
        }
    return {"role": msg.role, "content": msg.content or ""}


def _anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": _wire_tool_name(tool.name),
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def _anthropic_tool_choice(choice: ToolChoice, parallel_tool_calls: bool) -> dict[str, Any]:
    if choice.startswith("tool:"):
        out: dict[str, Any] = {"type": "tool",
                               "name": _wire_tool_name(choice[len("tool:"):])}
    elif choice == "required":
        out = {"type": "any"}
    elif choice == "none":
        out = {"type": "none"}
    else:
        out = {"type": "auto"}
    if not parallel_tool_calls:
        out["disable_parallel_tool_use"] = True
    return out


def _anthropic_message(msg: ChatMessage) -> dict[str, Any]:
    if msg.role == "assistant":
        content: list[dict[str, Any]] = []
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        for tc in msg.tool_calls:
            content.append(
                {"type": "tool_use", "id": tc.id,
                 "name": _wire_tool_name(tc.name), "input": tc.arguments}
            )
        return {"role": "assistant", "content": content}
    if msg.role == "tool":
        # Anthropic tool_result 는 OpenAI 의 role:"tool" 과 달리 *user 턴* 에 실린다.
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": msg.tool_call_id or "",
            "content": msg.content or "",
        }
        if msg.is_error:
            block["is_error"] = True
        return {"role": "user", "content": [block]}
    # user(system 은 호출부에서 top-level 로 승격되어 여기 도달하지 않음).
    return {"role": "user", "content": msg.content or ""}


def _map_anthropic_stop_reason(reason: str | None) -> str:
    """Anthropic vocabulary → OpenAI vocabulary. `end_turn` is the normal
    completion; `max_tokens` matches; tool/stop-sequence map to their
    OpenAI counterparts."""
    if reason is None:
        return "stop"
    if reason in ("end_turn", "stop_sequence"):
        return "stop"
    if reason == "max_tokens":
        return "length"
    if reason == "tool_use":
        return "tool_calls"
    return reason


# OpenAI-compat /chat/completions 가 받는 추가 샘플링·포맷 파라메터 화이트리스트.
# registry model_options 에 선언된 키 중 이 집합만 와이어로 전달한다(temperature·
# max_tokens 는 호출부에서 이미 pop 해 별도 처리). 화이트리스트로 한정해 오타·미지원
# 키가 4xx 를 유발하지 않게 하고, 재현성(seed)·구조 제어(response_format·stop·
# top_p 등)를 registry 선언만으로 켤 수 있게 한다. vLLM·OpenAI 공통 top-level 필드.
_OPENAI_SAMPLING_KEYS = frozenset({
    "top_p", "top_k", "frequency_penalty", "presence_penalty",
    "seed", "stop", "response_format", "min_p", "repetition_penalty",
    "logprobs", "top_logprobs", "n",
})


def _sampling_extras(opts: dict[str, Any]) -> dict[str, Any]:
    """남은 model_options(temperature·max_tokens pop 후)에서 화이트리스트 키만 추려
    OpenAI-compat payload 에 머지할 dict 로 돌려준다. 미지원/오타 키는 조용히 버린다
    (와이어 4xx 방지). grammar(guided_*) 는 별도 경로라 여기 포함하지 않는다."""
    return {k: v for k, v in opts.items() if k in _OPENAI_SAMPLING_KEYS and v is not None}


def _apply_grammar_to_openai_payload(
    payload: dict[str, Any], grammar: GrammarSpec | None
) -> None:
    """Translate a `GrammarSpec` into vLLM guided-decoding kwargs.

    vLLM accepts `guided_grammar` / `guided_regex` / `guided_json` /
    `guided_choice` as top-level body fields (its OpenAI-compat server
    forwards them to the sampling engine — XGrammar/Outlines).
    Upstreams that don't recognise these keys ignore them harmlessly.

    "json_schema" maps to the OpenAI `response_format` field *as well*
    so cloud OpenAI honours it; vLLM accepts either form. Keeping both
    sides covered means the same call works against both endpoints.
    """
    if grammar is None:
        return
    if grammar.kind == "grammar":
        payload["guided_grammar"] = grammar.value
    elif grammar.kind == "regex":
        payload["guided_regex"] = grammar.value
    elif grammar.kind == "json_schema":
        payload["guided_json"] = grammar.value
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "guided", "schema": grammar.value},
        }
    elif grammar.kind == "choice":
        payload["guided_choice"] = list(grammar.value)


class _Retry5xx(Exception):
    pass


def _raise_for_status(resp: httpx.Response) -> None:
    if 500 <= resp.status_code < 600:
        raise _Retry5xx(f"upstream {resp.status_code}: {resp.text[:256]}")
    if resp.status_code >= 400:
        # 4xx is a permanent failure (bad request / auth) — surface as unavailable.
        raise LLMUnavailableError(f"upstream {resp.status_code}: {resp.text[:256]}")


# ── streaming frame decoders (Anthropic SSE vs Bedrock event-stream) ──────────


async def _iter_anthropic_sse(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """First-party Anthropic streaming: plain SSE. Yield each chunk-event dict
    from `data:` lines (skip `event:`/comment/blank framing — we route on
    `data.type`)."""
    async for line in resp.aiter_lines():
        if not line or line.startswith(":") or line.startswith("event:"):
            continue
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str:
            continue
        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            continue


async def _iter_bedrock_event_stream(
    resp: httpx.Response,
) -> AsyncIterator[dict[str, Any]]:
    """Bedrock `invoke-with-response-stream`: each AWS event-stream binary frame
    carries a `chunk` event whose payload is JSON `{"bytes": "<base64>"}`, and
    the base64 decodes to one Anthropic SSE chunk-event JSON. Decode the binary
    framing with botocore's EventStreamBuffer, then unwrap to the chunk dict.

    botocore exception frames (modelStreamErrorException, throttlingException,
    …) surface as a synthetic `{"type": "error", ...}` so the caller's existing
    error branch handles them."""
    import base64

    from botocore.eventstream import EventStreamBuffer

    buffer = EventStreamBuffer()
    async for raw in resp.aiter_bytes():
        if not raw:
            continue
        buffer.add_data(raw)
        for event in buffer:
            headers = event.headers
            message_type = headers.get(":message-type")
            event_type = headers.get(":event-type")
            payload = event.payload or b""
            if message_type in ("exception", "error"):
                try:
                    detail = json.loads(payload.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    detail = {}
                yield {
                    "type": "error",
                    "error": {
                        "type": str(
                            headers.get(":exception-type") or event_type or "error"
                        ),
                        "message": str(detail.get("message") or payload[:256]),
                    },
                }
                continue
            if not payload:
                continue
            try:
                frame = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            inner = frame.get("bytes")
            if not inner:
                continue
            try:
                yield json.loads(base64.b64decode(inner).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue


# ── Bedrock SigV4 signer ──────────────────────────────────────────────────────


class _BedrockSigner:
    """SigV4 signer for `bedrock-runtime` POST requests, backed by botocore's
    credential resolution (env `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/
    `AWS_SESSION_TOKEN`, shared profile, or IAM role) and SigV4Auth. Resolves
    credentials once at construction; returns the signed headers to merge onto
    the httpx request."""

    def __init__(self, region: str) -> None:
        from botocore.auth import SigV4Auth
        from botocore.session import Session

        session = Session()
        credentials = session.get_credentials()
        if credentials is None:
            raise LLMUnavailableError(
                "bedrock: no AWS credentials resolved (set AWS_ACCESS_KEY_ID/"
                "AWS_SECRET_ACCESS_KEY or attach an IAM role)"
            )
        self._region = region
        self._auth = SigV4Auth(credentials, _BEDROCK_SERVICE, region)

    def sign(self, url: str, body: bytes) -> dict[str, str]:
        from botocore.awsrequest import AWSRequest

        request = AWSRequest(
            method="POST",
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        self._auth.add_auth(request)
        # SigV4Auth mutates request.headers in place with Authorization +
        # X-Amz-Date (+ X-Amz-Security-Token when using temporary creds).
        return dict(request.headers)
