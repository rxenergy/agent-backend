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

# Cap attribute size to keep spans well under collector limits (~32KB total).
_MAX_VALUE_BYTES = 8192


def _truncate(s: str, limit: int = _MAX_VALUE_BYTES) -> str:
    b = s.encode("utf-8", errors="replace")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", errors="ignore") + "...[truncated]"


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
