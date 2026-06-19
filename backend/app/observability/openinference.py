"""OpenInference semantic conventions for Phoenix UI.

Phoenix renders span input/output inline when spans carry the OpenInference
attribute schema (https://github.com/Arize-ai/openinference). This module
provides the minimal constants + helpers we need; we don't depend on the
`openinference-semantic-conventions` package to keep the image lean.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from opentelemetry.trace import Span

# Span kinds (Phoenix expects these exact strings; OpenInference traces spec).
KIND_AGENT = "AGENT"
KIND_CHAIN = "CHAIN"
KIND_LLM = "LLM"
KIND_RETRIEVER = "RETRIEVER"
KIND_TOOL = "TOOL"
# 게이트/검증 노드(Node 6 retrieval_evaluate · Node 15 claim_verify)는 채점·판정이
# 본질이라 CHAIN 이 아니라 EVALUATOR; 라우팅·거부 게이트(Node 2 scenario_routing)는
# GUARDRAIL. Phoenix 가 이 kind 로 노드 역할을 구분 렌더한다(실패 귀인).
KIND_EVALUATOR = "EVALUATOR"
KIND_GUARDRAIL = "GUARDRAIL"

# Attribute keys.
SPAN_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
INPUT_MIME = "input.mime_type"
OUTPUT_VALUE = "output.value"
OUTPUT_MIME = "output.mime_type"

LLM_MODEL_NAME = "llm.model_name"
LLM_INPUT_MESSAGES = "llm.input_messages"  # prefix
LLM_OUTPUT_MESSAGES = "llm.output_messages"  # prefix
LLM_TOKEN_PROMPT = "llm.token_count.prompt"
LLM_TOKEN_COMPLETION = "llm.token_count.completion"
LLM_TOKEN_TOTAL = "llm.token_count.total"

RETRIEVAL_DOCUMENTS = "retrieval.documents"  # prefix

TOOL_NAME = "tool.name"
TOOL_PARAMETERS = "tool.parameters"

# span 속성 값 크기 상한(문자 수). 슬롯 파이프라인(composer_pipelined)은 full chunk body 를
# 담은 슬롯 프롬프트를 슬롯마다 LLM span 에 싣는데, 본문이 수십~수백 KB 라 한 트레이스가 수
# MB 까지 부푼다 → Phoenix 렌더 지연·gRPC 메시지 한도 초과·Tempo 저장 비용. 콜렉터에 span
# attribute limit 이 없으므로(infra/otel/collector.yaml) 앱 단에서 1차로 막는다. 전체 본문은
# context_snapshot(artifact store) + rendered_prompt_hash 로 재현되므로 span 은 미리보기면
# 충분하다(재현은 해시·아티팩트, 관측은 미리보기 — 책임 분리).
#
# 기본 64KB — 통상 프롬프트(수 KB)는 무손실, 병리적 거대 본문만 자른다. 잘릴 때는 끝에
# 마커를 붙여 "잘림"이 디버깅에서 명시적이게 한다(조용한 절단 금지). 0/음수면 무제한
# (콜렉터 한도에만 의존 — 이전 동작). OTEL_SPAN_ATTR_MAX_CHARS 로 조절.
def _span_attr_max_chars() -> int:
    import os
    raw = os.getenv("OTEL_SPAN_ATTR_MAX_CHARS", "")
    if not raw.strip():
        return 65536
    try:
        return int(raw)
    except ValueError:
        return 65536


_TRUNC_MARKER = "\n…[truncated by OTEL_SPAN_ATTR_MAX_CHARS — full text in context_snapshot]"


def _truncate(s: str, limit: int | None = None) -> str:
    cap = limit if limit is not None else _span_attr_max_chars()
    if cap <= 0 or len(s) <= cap:
        return s
    return s[:cap] + _TRUNC_MARKER


def _as_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def set_kind(span: Span, kind: str) -> None:
    span.set_attribute(SPAN_KIND, kind)


def set_io(
    span: Span,
    *,
    input_value: Any = None,
    output_value: Any = None,
) -> None:
    if input_value is not None:
        text = _as_json(input_value)
        span.set_attribute(INPUT_VALUE, _truncate(text))
        span.set_attribute(INPUT_MIME, "application/json" if not isinstance(input_value, str) else "text/plain")
    if output_value is not None:
        text = _as_json(output_value)
        span.set_attribute(OUTPUT_VALUE, _truncate(text))
        span.set_attribute(OUTPUT_MIME, "application/json" if not isinstance(output_value, str) else "text/plain")


def set_llm(
    span: Span,
    *,
    model_name: str,
    prompt: str,
    completion: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    span.set_attribute(LLM_MODEL_NAME, model_name)
    span.set_attribute(f"{LLM_INPUT_MESSAGES}.0.message.role", "user")
    span.set_attribute(
        f"{LLM_INPUT_MESSAGES}.0.message.content", _truncate(prompt)
    )
    span.set_attribute(f"{LLM_OUTPUT_MESSAGES}.0.message.role", "assistant")
    span.set_attribute(
        f"{LLM_OUTPUT_MESSAGES}.0.message.content", _truncate(completion)
    )
    if prompt_tokens:
        span.set_attribute(LLM_TOKEN_PROMPT, int(prompt_tokens))
    if completion_tokens:
        span.set_attribute(LLM_TOKEN_COMPLETION, int(completion_tokens))
    if prompt_tokens or completion_tokens:
        span.set_attribute(LLM_TOKEN_TOTAL, int(prompt_tokens + completion_tokens))
    # Mirror to input.value/output.value so the Phoenix tile preview works too.
    set_io(span, input_value=prompt, output_value=completion)


def set_llm_chat(
    span: Span,
    *,
    model_name: str,
    input_messages: Sequence[tuple[str, str]],
    completion: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """멀티메시지(system+user 등) LLM 호출용 — `set_llm` 의 chat 판. `set_llm` 은
    단일 user 메시지만 싣지만, 참조 추출(generate_messages)은 system+user 를 보내므로
    각 입력 메시지를 role 과 함께 `llm.input_messages.{i}` 로 싣는다. Phoenix 가 이
    스키마로 입력 대화를 그대로 렌더한다(다른 LLM 노드와 동형)."""
    span.set_attribute(LLM_MODEL_NAME, model_name)
    for i, (role, content) in enumerate(input_messages):
        span.set_attribute(f"{LLM_INPUT_MESSAGES}.{i}.message.role", role)
        span.set_attribute(
            f"{LLM_INPUT_MESSAGES}.{i}.message.content", _truncate(content)
        )
    span.set_attribute(f"{LLM_OUTPUT_MESSAGES}.0.message.role", "assistant")
    span.set_attribute(
        f"{LLM_OUTPUT_MESSAGES}.0.message.content", _truncate(completion)
    )
    if prompt_tokens:
        span.set_attribute(LLM_TOKEN_PROMPT, int(prompt_tokens))
    if completion_tokens:
        span.set_attribute(LLM_TOKEN_COMPLETION, int(completion_tokens))
    if prompt_tokens or completion_tokens:
        span.set_attribute(LLM_TOKEN_TOTAL, int(prompt_tokens + completion_tokens))
    # Phoenix 타일 미리보기용 input.value/output.value 미러.
    set_io(
        span,
        input_value=[{"role": r, "content": c} for r, c in input_messages],
        output_value=completion,
    )


def set_retrieval_documents(
    span: Span,
    docs: Sequence[Mapping[str, Any]],
) -> None:
    """Each doc: {id, score, content, metadata?}."""
    for i, doc in enumerate(docs):
        base = f"{RETRIEVAL_DOCUMENTS}.{i}.document"
        if doc.get("id") is not None:
            span.set_attribute(f"{base}.id", str(doc["id"]))
        if doc.get("score") is not None:
            try:
                span.set_attribute(f"{base}.score", float(doc["score"]))
            except (TypeError, ValueError):
                pass
        if doc.get("content") is not None:
            span.set_attribute(
                f"{base}.content", _truncate(_as_json(doc["content"]))
            )
        meta = doc.get("metadata")
        if meta:
            span.set_attribute(
                f"{base}.metadata", _truncate(_as_json(meta))
            )


def set_tool(
    span: Span,
    *,
    name: str,
    parameters: Any = None,
    output: Any = None,
) -> None:
    span.set_attribute(TOOL_NAME, name)
    if parameters is not None:
        span.set_attribute(TOOL_PARAMETERS, _truncate(_as_json(parameters)))
    set_io(span, input_value=parameters, output_value=output)
