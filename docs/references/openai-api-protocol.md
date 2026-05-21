---
title: OpenAI API Protocol — 장기 레퍼런스
node_id: docs/references/openai-api-protocol
category: references
type: protocol-reference
status: living
created: 2026-05-12
audience: implementation, ops, future-maintainers
related:
  - docs/agent-saas-mvp-specification.md (§8.3 원칙 2, §11)
  - docs/plans/backend-openai-api-scope.md
upstream:
  - https://platform.openai.com/docs/api-reference
  - https://github.com/openai/openai-openapi
---

# OpenAI API Protocol — 장기 레퍼런스

> 본 문서는 **OpenAI API 자체의 외부 사양**을 본 프로젝트가 장기적으로 참조 가능한 형태로 정리한다. **본 백엔드가 무엇을 구현하는지는 다루지 않는다** — 그것은 `docs/plans/backend-openai-api-scope.md`의 책임이다.
>
> 본 문서의 권위 원천은 OpenAI 공식 문서(`platform.openai.com/docs/api-reference`)와 공개 OpenAPI 스펙(`github.com/openai/openai-openapi`). 본문은 그 사양 중 **본 MVP의 통합 경계(OpenWebUI ↔ Backend ↔ 외부 LLM)에 관련된 부분**만 정리한다.
>
> OpenAI는 API를 비교적 자주 확장한다(예: Responses API 도입, `annotations` 추가, `reasoning_tokens` 필드). 본 문서는 **living document**이며, 마지막 갱신 시점의 안정 표면을 기록한다. 변경 추적은 §10.

## 0. 문서 사용 규칙

- **신뢰 우선순위**: OpenAI 공식 문서 > OpenAPI 스키마 > 본 문서 > 본 백엔드 구현. 충돌 시 위가 정답.
- 본 문서가 "필드가 있다"고 적었다는 사실은 본 백엔드가 그것을 구현한다는 의미가 **아니다**. 구현 범위는 `backend-openai-api-scope.md`만 결정한다.
- 본 문서의 모든 엔드포인트 경로는 `/v1`을 기준으로 한다. OpenAI는 일부 신규 표면에서 `/v1` 외 prefix(예: `/v2/...`)를 도입할 수 있으나, Chat Completions 라인은 `/v1`이 안정 경로다.

---

## 1. 프로토콜 개요

OpenAI API는 **HTTPS + JSON + Bearer 인증** 기반의 REST API. 일부 엔드포인트는 **SSE(Server-Sent Events)** 로 점진적 응답을 제공한다.

### 1.1 핵심 엔드포인트 인벤토리

| 분류 | 엔드포인트 | 본 MVP 관련도 | 비고 |
|------|-----------|--------------|------|
| **Chat** | `POST /v1/chat/completions` | ★★★ 핵심 | OpenWebUI ↔ Backend 주 채널 |
| **Chat (신규)** | `POST /v1/responses` | ☆ 관찰만 | OpenAI가 미는 차세대 API, 호환 생태계는 아직 Chat Completions 위주 |
| **Models** | `GET /v1/models`, `GET /v1/models/{id}` | ★★ | OpenWebUI 부팅 시 모델 목록 |
| **Embeddings** | `POST /v1/embeddings` | ★ (사용 안 함) | 인덱싱은 OpenSearch ML Commons 사용 |
| **Moderations** | `POST /v1/moderations` | — | 본 MVP는 도메인 evaluator로 대체 |
| **Audio** | `POST /v1/audio/transcriptions`, `…/translations`, `…/speech` | — | 비목표 |
| **Images** | `POST /v1/images/generations`, `…/edits`, `…/variations` | — | 비목표 |
| **Files** | `POST /v1/files`, `GET /v1/files`, … | — | 비목표 |
| **Batches** | `POST /v1/batches` | — | 비목표 |
| **Fine-tuning** | `POST /v1/fine_tuning/jobs` | — | Phase 2 후보 (DPO 파이프라인) |
| **Assistants v2** | `POST /v1/assistants`, `…/threads`, `…/runs` | — | OpenAI가 Responses API로 이전 중 |

### 1.2 인증

```
Authorization: Bearer <api-key>
OpenAI-Organization: <org-id>          (선택)
OpenAI-Project: <project-id>           (선택, project-scoped key 사용 시)
OpenAI-Beta: <feature-flag>            (특정 베타 기능 활성화 시)
```

- `Authorization` 외 헤더는 OpenAI 공식 서버에만 의미가 있다.
- **OpenAI-호환 서버**(vLLM, LiteLLM, TGI, 본 백엔드 등)는 보통 `Authorization`만 검증한다.

### 1.3 Base URL

| 대상 | 기본 URL |
|------|---------|
| OpenAI 공식 | `https://api.openai.com/v1` |
| Azure OpenAI | `https://{resource}.openai.azure.com/openai/deployments/{deployment}` + `api-version` 쿼리 (스키마 동일, 경로 다름) |
| vLLM/TGI/LiteLLM/사내 백엔드 | 임의 호스트 + `/v1` 경로 유지 |

OpenAI-호환 클라이언트(OpenWebUI 포함)는 `base_url` + `api_key`만으로 다른 호환 서버를 가리킬 수 있다 — 이것이 본 MVP의 통합 모델이다.

---

## 2. Chat Completions API

본 프로젝트에서 가장 중요한 단일 엔드포인트.

### 2.1 Endpoint

```
POST /v1/chat/completions
Content-Type: application/json
Authorization: Bearer <key>
```

### 2.2 Request 스키마 — 전체 필드

| 필드 | 타입 | 필수 | 의미 |
|------|------|------|------|
| `model` | string | ✅ | 사용할 모델 ID |
| `messages` | array<Message> | ✅ | 대화 이력 (아래 §2.3) |
| `stream` | bool | — | true면 SSE로 chunk 스트리밍 (기본 false) |
| `stream_options` | object | — | `{include_usage: bool}` — 스트리밍 종료 chunk에 usage 포함 |
| `max_tokens` | int | — | 응답 토큰 상한 (deprecated, 신규 모델은 `max_completion_tokens`) |
| `max_completion_tokens` | int | — | 응답 토큰 상한 (reasoning 토큰 포함) |
| `temperature` | float [0, 2] | — | 샘플링 온도. 기본 1.0 |
| `top_p` | float [0, 1] | — | nucleus sampling. `temperature`와 둘 중 하나만 권장 |
| `n` | int ≥1 | — | 생성할 응답 개수. 기본 1 |
| `stop` | string \| string[] | — | 정지 시퀀스 (최대 4개) |
| `presence_penalty` | float [-2, 2] | — | 새 토큰 등장 페널티 |
| `frequency_penalty` | float [-2, 2] | — | 빈도 페널티 |
| `logit_bias` | map<token_id, float> | — | 토큰별 로그확률 편향 [-100, 100] |
| `logprobs` | bool | — | 로그확률 반환 여부 |
| `top_logprobs` | int [0, 20] | — | 각 위치에서 반환할 상위 토큰 수 (logprobs=true 시) |
| `seed` | int | — | 결정론적 샘플링 힌트. 동일 seed + 동일 입력 → 동일 출력(베스트 에포트) |
| `response_format` | object | — | `{type: "text"}` \| `{type: "json_object"}` \| `{type: "json_schema", json_schema: {...}}` |
| `tools` | array<Tool> | — | 함수 도구 정의 (§2.7) |
| `tool_choice` | string \| object | — | `"none"` \| `"auto"` \| `"required"` \| `{type: "function", function: {name}}` |
| `parallel_tool_calls` | bool | — | 한 응답에서 도구 병렬 호출 허용. 기본 true |
| `user` | string | — | 최종 사용자 식별자 (남용 모니터링용) |
| `metadata` | object<string,string> | — | 자유 키-값. 저장된 응답 검색 시 사용 |
| `store` | bool | — | OpenAI 측 응답 저장 여부 (공식 API 전용) |
| `service_tier` | string | — | `"auto"` \| `"default"` \| `"flex"` (공식 API 전용) |
| `prediction` | object | — | 예측 출력 힌트 (Predicted Outputs 기능, 일부 모델) |
| `audio` | object | — | 오디오 출력 옵션 `{voice, format}` (오디오 지원 모델) |
| `modalities` | string[] | — | `["text"]` \| `["text", "audio"]` 등 |
| `reasoning_effort` | string | — | `"low"` \| `"medium"` \| `"high"` (reasoning 모델 전용) |

### 2.3 Message 객체

```
{
  "role": "system" | "user" | "assistant" | "tool" | "developer",
  "content": string | ContentPart[],
  "name": string,                  // 선택
  "tool_call_id": string,          // role=tool 시 필수
  "tool_calls": ToolCall[],        // role=assistant 시 (이전 turn에서 도구 호출했을 때)
  "refusal": string                // role=assistant 시 (모델이 거절)
}
```

**Role 의미**:
- `system`: 시스템 지시 (deprecated 방향, 신규 모델은 `developer` 권장)
- `developer`: 시스템 지시의 후속 명칭
- `user`: 최종 사용자 메시지
- `assistant`: 모델 응답 (이력에 포함)
- `tool`: 도구 실행 결과 (이전 turn의 `tool_calls`에 대응)

**ContentPart 타입** (멀티모달):

```
text:        { "type": "text", "text": "..." }
image_url:   { "type": "image_url", "image_url": { "url": "https://..." | "data:image/png;base64,..." , "detail": "low|high|auto" } }
input_audio: { "type": "input_audio", "input_audio": { "data": "<base64>", "format": "wav|mp3" } }
file:        { "type": "file", "file": { "file_id": "..." } }
```

### 2.4 Response 스키마 — 비스트리밍

```jsonc
{
  "id": "chatcmpl-xxx",              // 응답 ID. 호환 서버는 임의 UUID 사용 가능
  "object": "chat.completion",
  "created": 1715500000,             // Unix timestamp
  "model": "gpt-4o-2024-08-06",
  "system_fingerprint": "fp_xxx",    // 모델 + 인프라 버전 지문 (선택)
  "service_tier": "default",         // 공식 API에서만
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "..." | null,       // tool_calls만 있을 때 null
      "refusal": null | "거절 사유",
      "tool_calls": null | ToolCall[],
      "annotations": null | Annotation[],  // url_citation 등 (신규)
      "audio": null | { "id", "data", "transcript", "expires_at" }
    },
    "logprobs": null | { "content": [...], "refusal": [...] },
    "finish_reason": "stop" | "length" | "tool_calls" | "content_filter" | "function_call"
  }],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "prompt_tokens_details": { "cached_tokens": 0, "audio_tokens": 0 },
    "completion_tokens_details": { "reasoning_tokens": 0, "audio_tokens": 0, "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0 }
  }
}
```

### 2.5 `finish_reason` 값

| 값 | 의미 |
|----|------|
| `stop` | 자연 종료 또는 stop 시퀀스 매치 |
| `length` | `max_tokens` 도달로 잘림 |
| `tool_calls` | 모델이 도구 호출을 요청하여 종료 |
| `content_filter` | 안전 필터에 의해 차단 |
| `function_call` | (deprecated) legacy function calling |

호환 서버는 위 값을 정확히 사용해야 한다. 임의 값을 넣으면 클라이언트 파서가 깨질 수 있다.

### 2.6 스트리밍 (SSE)

`stream: true`일 때:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":...,"model":"...","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"안녕"},"finish_reason":null}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"하세요"},"finish_reason":null}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"chatcmpl-x","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}

data: [DONE]
```

규칙:
- 각 이벤트는 `data: <json>\n\n` 형식.
- `object`는 `chat.completion.chunk`.
- `choices[].delta`는 *증분* (full 메시지가 아님). 클라이언트가 누적해야 한다.
- 모든 chunk의 `id`는 동일.
- 첫 chunk는 보통 `delta.role`만 있고 `content`는 비어 있다.
- 마지막 본문 chunk는 `finish_reason`을 채운다.
- `stream_options.include_usage=true`이면 `finish_reason` chunk 다음에 `choices=[]`이고 `usage`만 채운 chunk가 추가된다.
- **종료 마커는 `data: [DONE]\n\n`** (JSON 아님). 이게 없으면 일부 클라이언트가 연결을 정리하지 않는다.
- 도구 호출 스트리밍 시 `delta.tool_calls[].function.arguments`도 증분으로 온다.

### 2.7 Tool / Function Calling

**Tool 정의 (request)**:

```jsonc
{
  "tools": [{
    "type": "function",
    "function": {
      "name": "search_regulation",
      "description": "원자력 규제 조항을 검색한다",
      "parameters": {
        "type": "object",
        "properties": {
          "query": { "type": "string" },
          "top_k": { "type": "integer", "default": 5 }
        },
        "required": ["query"]
      },
      "strict": true                    // (선택) JSON Schema 엄격 강제
    }
  }],
  "tool_choice": "auto"
}
```

**Response (tool call)**:

```jsonc
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc",
        "type": "function",
        "function": {
          "name": "search_regulation",
          "arguments": "{\"query\":\"정기검사\",\"top_k\":5}"   // JSON string, 객체 아님
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

**다음 turn — 도구 결과 회신**:

```jsonc
{
  "messages": [
    /* …이전 turn… */,
    { "role": "assistant", "tool_calls": [{ "id": "call_abc", ... }] },
    { "role": "tool", "tool_call_id": "call_abc", "content": "<JSON 결과 문자열>" }
  ]
}
```

규칙:
- `function.arguments`는 **JSON 문자열**(파싱은 클라이언트 책임). 모델이 항상 valid JSON을 보장하지는 않으므로 `strict: true` 권장.
- `parallel_tool_calls: true` (기본)이면 한 응답에 여러 `tool_calls` 가능.
- 응답 turn에서 `role: "tool"` 메시지는 호출된 모든 `tool_call_id`에 대해 제공되어야 함.

### 2.8 Structured Outputs (`response_format`)

```jsonc
{
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "citation_extraction",
      "schema": { "type": "object", "properties": { ... }, "required": [...], "additionalProperties": false },
      "strict": true
    }
  }
}
```

- `strict: true`이면 모델 출력이 schema를 엄격 준수. 단 일부 JSON Schema 기능(예: `oneOf` 일부)은 미지원.
- 구버전: `{"type": "json_object"}`만 — 스키마는 강제되지 않고 "JSON 형식 출력만" 보장.

### 2.9 Vision / 멀티모달 입력

```jsonc
{
  "messages": [{
    "role": "user",
    "content": [
      { "type": "text", "text": "이 이미지를 설명해줘" },
      { "type": "image_url", "image_url": { "url": "data:image/png;base64,...", "detail": "high" } }
    ]
  }]
}
```

`detail`:
- `low`: 저해상도 처리, 토큰 절감
- `high`: 원본 해상도 분할 처리
- `auto`: 모델 판단

### 2.10 오디오 / 예측 출력 / Reasoning

- **Audio output**: `modalities: ["text", "audio"]` + `audio: {voice: "alloy", format: "wav"}` → response의 `message.audio`에 base64 데이터.
- **Predicted Outputs**: `prediction: {type: "content", content: "<예상 출력 일부>"}` → 모델이 예상과 일치하는 부분을 캐시 가속 (대규모 편집 시 유용).
- **Reasoning models** (o-series): `reasoning_effort: "low|medium|high"`, 응답 `usage.completion_tokens_details.reasoning_tokens`에 사고 토큰 수 노출.

---

## 3. Models API

### 3.1 GET /v1/models

```jsonc
{
  "object": "list",
  "data": [
    { "id": "gpt-4o", "object": "model", "created": 1715500000, "owned_by": "openai" },
    { "id": "gpt-4o-mini", "object": "model", "created": 1715500000, "owned_by": "openai" }
  ]
}
```

- 호환 서버는 `created`와 `owned_by`를 자유롭게 채울 수 있으나 필드는 존재해야 한다.
- OpenWebUI는 부팅 시 1회 호출하여 모델 드롭다운을 채운다.

### 3.2 GET /v1/models/{id}

단일 모델 객체 반환. OpenWebUI는 보통 호출하지 않음.

---

## 4. Embeddings API (참고용)

```
POST /v1/embeddings
{
  "model": "text-embedding-3-small",
  "input": "...텍스트..." | ["문자열 배열"],
  "encoding_format": "float" | "base64",
  "dimensions": 1536                     // 선택 (모델이 지원할 때)
}
```

응답:

```jsonc
{
  "object": "list",
  "data": [{ "object": "embedding", "index": 0, "embedding": [0.001, ...] }],
  "model": "text-embedding-3-small",
  "usage": { "prompt_tokens": 5, "total_tokens": 5 }
}
```

본 MVP는 임베딩을 OpenSearch ML Commons 측에서 처리하므로 이 엔드포인트는 사용하지 않는다 (§agent-saas-mvp-specification.md §F2).

---

## 5. 에러 응답 표준

모든 엔드포인트의 에러는 동일 포맷:

```jsonc
HTTP/1.1 4xx 또는 5xx
Content-Type: application/json

{
  "error": {
    "message": "사람이 읽을 수 있는 설명",
    "type": "invalid_request_error" | "authentication_error" | "permission_error"
                | "not_found_error" | "rate_limit_error" | "api_error" | "overloaded_error",
    "param": "messages" | null,
    "code": "model_not_found" | "context_length_exceeded" | ... | null
  }
}
```

| HTTP | 의미 | 일반적 `type` |
|------|------|--------------|
| 400 | 요청 스키마 오류 | `invalid_request_error` |
| 401 | 인증 실패 | `authentication_error` |
| 403 | 권한 거부 | `permission_error` |
| 404 | 모델/리소스 미존재 | `not_found_error` |
| 422 | 의미적 검증 실패 | `invalid_request_error` |
| 429 | 레이트 리밋 / 쿼터 | `rate_limit_error` |
| 500 | 서버 내부 오류 | `api_error` |
| 503 | 과부하 | `overloaded_error` |

호환 서버도 위 포맷을 따라야 한다. OpenWebUI는 `error.message`를 그대로 사용자에게 노출한다.

---

## 6. Rate Limit 헤더

OpenAI 공식 응답:

```
x-ratelimit-limit-requests:    10000
x-ratelimit-limit-tokens:      1000000
x-ratelimit-remaining-requests: 9999
x-ratelimit-remaining-tokens:   999000
x-ratelimit-reset-requests:    "1s"
x-ratelimit-reset-tokens:      "60ms"
retry-after:                   "20"        (429 응답 시)
```

호환 서버는 이를 제공하지 않아도 무방하나, 제공 시 클라이언트의 백오프가 더 적절해진다.

---

## 7. Responses API (`/v1/responses`) — 관찰만

OpenAI가 2024년 말부터 미는 **차세대 API**. Assistants v2와 Chat Completions의 통합 후속이며, 다음을 한 엔드포인트에 통합한다:
- 대화 상태 서버 측 보관 (`previous_response_id`로 연결)
- 도구 호출 (function + 내장 도구: web search, file search, computer use)
- 멀티모달 입출력
- Reasoning 모델 통합

**스키마는 Chat Completions와 호환되지 않는다** (`input` 필드, `output` 배열 구조, `instructions` 등 다름).

본 MVP의 입장:
- **사용하지 않는다.** OpenWebUI 및 다수 호환 클라이언트는 Chat Completions 기반.
- 모니터링: OpenAI가 Chat Completions를 deprecate할 가능성은 가까운 시일 내 낮으나, 호환 생태계 동향은 추적 대상.

---

## 8. OpenAI-호환 서버 생태계

본 MVP가 의존하는 *외부* OpenAI 호환 구현 목록:

| 구현 | 역할 | 호환 범위 |
|------|------|-----------|
| **vLLM** | 온프레미스 오픈 모델 서빙 | Chat Completions, Models, Embeddings, 일부 tool calling |
| **TGI (Text Generation Inference)** | 대체 서빙 옵션 | Chat Completions (제한적), generate (자체) |
| **LiteLLM** | 다중 provider proxy | Chat Completions 완전, /v1/chat/completions로 Anthropic/Google 통합 가능 |
| **llama.cpp server** | 경량 로컬 | Chat Completions 기본 |
| **OpenWebUI** | 클라이언트 측 | Chat Completions, Models, 스트리밍 |

**호환성 한계**:
- 호환 서버 대부분은 `tools`, `response_format`, `logprobs`를 부분 지원 또는 미지원.
- `system_fingerprint`, `service_tier`, `store`는 거의 모든 호환 서버에서 무시 또는 미반환.
- 본 MVP의 백엔드도 호환 서버 중 하나로 동작 — 같은 한계를 의식하고 좁은 표면만 약속할 것.

---

## 9. 본 MVP 통합 경계에서 의미 있는 표면 요약

본 문서의 모든 사양 중, OpenWebUI ↔ Backend 통합에서 **실제로 통신하는** 부분만 따로 정리:

| 표면 | 사용 방향 | 본 MVP에서의 위상 |
|------|----------|------------------|
| `GET /v1/models` | OpenWebUI → Backend | 부팅 시 1회. 응답 필수 |
| `POST /v1/chat/completions` (비스트리밍) | OpenWebUI → Backend | 채팅 메시지마다 |
| `POST /v1/chat/completions` (스트리밍) | OpenWebUI → Backend | Phase 1 선택, Phase 2 권장 |
| `Authorization: Bearer` | 양방향 | 내부 공유 비밀 |
| Response `id` 필드 | Backend → OpenWebUI | `interaction_id` 오버로드 (spec §10.6) |
| Response `choices[0].message.content` | Backend → OpenWebUI | Markdown 본문 + 인용 footnote |
| Response `usage` | Backend → OpenWebUI | 값 0 허용, 필드는 존재 |
| Response `model` 필드 | Backend → OpenWebUI | 실제 라우팅된 변형 ID 노출 (A/B 추적용) |
| 에러 응답 포맷 | Backend → OpenWebUI | §5 표준 준수 필수 |
| `tools` / `tool_calls` | — | 부록 A §1 미결정, Phase 2 후보 |
| `response_format` | — | 미사용 |
| `n > 1`, `logprobs`, `seed` | — | 평가 파이프라인용 후보 |
| 멀티모달 content | — | 비목표 |

> 더 상세한 구현 범위 정의는 `docs/plans/backend-openai-api-scope.md` 참조.

---

## 10. 변경 추적

OpenAI는 다음 채널로 사양을 변경한다. 본 문서를 갱신할 때 우선 점검:

| 채널 | 용도 |
|------|------|
| `platform.openai.com/docs/changelog` | API 변경 공식 발표 |
| `github.com/openai/openai-openapi` (releases / commits) | 스키마 변화 |
| `platform.openai.com/docs/deprecations` | 폐기 일정 |
| OpenAI 블로그 / 모델 출시 공지 | 신규 표면(reasoning, audio 등) |

### 10.1 본 문서 갱신 트리거

다음 중 하나가 발생하면 본 문서를 갱신한다:
1. OpenAI가 Chat Completions의 필수 필드 또는 응답 구조를 변경.
2. OpenAI가 Chat Completions의 deprecation 일정을 발표.
3. 본 MVP가 새 OpenAI 기능(tool calling, structured outputs 등)을 도입하기로 결정 → 해당 절을 정밀화.
4. OpenWebUI 메이저 업그레이드로 클라이언트 측 의존 표면이 변경.

갱신 시 frontmatter `created`를 유지하고, 본문 말미에 §11 변경 기록을 추가한다.

---

## 11. 변경 기록

| 일자 | 변경 | 작성 |
|------|------|------|
| 2026-05-12 | 초안 작성. Chat Completions 중심 정리, Responses API는 관찰만 | (생성) |

---

## 부록 A. 관련 사양 / 외부 링크

- OpenAI API 레퍼런스: `https://platform.openai.com/docs/api-reference`
- OpenAI OpenAPI 스펙: `https://github.com/openai/openai-openapi`
- OpenAI Cookbook: `https://github.com/openai/openai-cookbook`
- OpenWebUI 문서: `https://docs.openwebui.com`
- vLLM OpenAI-호환 서버: `https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html`
- LiteLLM proxy: `https://docs.litellm.ai`

## 부록 B. 본 문서가 다루지 않는 것

- 본 백엔드의 *구현* 약속 → `backend-openai-api-scope.md`
- OpenWebUI의 환경변수 전체 목록 → `frontend/Dockerfile` 및 OpenWebUI 공식 문서
- Azure OpenAI / Bedrock의 OpenAI-호환 변형 세부 → 필요 시 별도 문서
- Anthropic / Google API의 자체 스펙 → 본 백엔드의 어댑터 구현 내부 관심사
