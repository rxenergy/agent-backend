from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel

from app.application.tool_runtime.errors import (
    RequiredToolFailed,
    ToolFailure,
    ToolTimeout,
    ToolUnknown,
)
from app.application.tool_runtime.registry import ToolRegistry, ToolSpec
from app.domain.tools import ToolErrorCode, ToolResult, ToolStatus
from app.observability import openinference as oi
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.tool import Tool, ToolExecutionContext

_TRACER = get_tracer("tool")


# 검색 span 에 실을 문서 상한 — 점수 분포 진단엔 상위 N 이면 충분하고, 전량(top_k 가 수십~
# 수백)을 속성으로 펴면 span 이 다시 부푼다(C1 과 같은 부담). RETRIEVER 미리보기 목적.
_RETRIEVAL_DOC_SPAN_CAP = 30


def _enrich_retrieval_span(span: Any, output: Any) -> None:
    """검색류 도구 output({"chunks": [...]})을 OpenInference RETRIEVER 스키마로 span 에 단다.

    chunks 가 없으면(검색 아님) 아무것도 하지 않는다 — 모든 도구를 공통 경로로 통과시키되
    검색만 추가 enrich(원칙 2 — 도구는 정책, executor 는 횡단 관측). 본문(content)은 싣지
    않는다(C1 — 거대 본문 회피); id/score/source 만 단다. 상위 N 개로 제한."""
    if not isinstance(output, dict):
        return
    chunks = output.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return
    oi.set_kind(span, oi.KIND_RETRIEVER)
    docs: list[dict[str, Any]] = []
    for raw in chunks[:_RETRIEVAL_DOC_SPAN_CAP]:
        if not isinstance(raw, dict):
            continue
        docs.append({
            "id": raw.get("chunk_id"),
            "score": raw.get("score"),
            # content 는 생략(본문 거대 — 미리보기는 id/score 로 충분, 본문은 artifact).
            "metadata": {
                k: raw.get(k) for k in ("document_id", "source_id", "page")
                if raw.get(k) is not None
            },
        })
    if docs:
        oi.set_retrieval_documents(span, docs)
    span.set_attribute("retrieval.num_chunks", len(chunks))


def _hash_payload(payload: Any) -> str:
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class ToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        tools: dict[str, Tool],
        event_sink: EventSinkPort,
    ) -> None:
        self._registry = registry
        self._tools = tools
        self._sink = event_sink

    async def invoke(
        self,
        name: str,
        tool_input: BaseModel | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        spec = self._registry.get(name)
        tool = self._tools.get(name)
        if tool is None:
            raise ToolUnknown(f"Tool not wired: {name}")

        input_hash = _hash_payload(tool_input)
        started = time.monotonic()
        retry_count = 0
        last_error: ToolFailure | None = None

        with _TRACER.start_as_current_span(f"tool.{spec.adapter}.{name}") as span:
            span.set_attribute("tool.name", name)
            span.set_attribute("tool.version", spec.version)
            span.set_attribute("tool.adapter", spec.adapter)
            span.set_attribute("tool.required", spec.required)
            span.set_attribute("tool.input_hash", input_hash)
            span.set_attribute("interaction_id", context.interaction_id)
            oi.set_kind(span, oi.KIND_TOOL)
            _params = (
                tool_input.model_dump(mode="json")
                if isinstance(tool_input, BaseModel)
                else tool_input
            )
            oi.set_tool(span, name=name, parameters=_params)

            attempts = max(1, 1 + spec.retry)
            for attempt in range(attempts):
                retry_count = attempt
                try:
                    result = await asyncio.wait_for(
                        tool.invoke(tool_input, context),
                        timeout=spec.timeout_ms / 1000,
                    )
                    output_hash = _hash_payload(result.output or {})
                    latency_ms = int((time.monotonic() - started) * 1000)
                    final = result.model_copy(
                        update={
                            "tool_name": name,
                            "tool_version": spec.version,
                            "input_hash": input_hash,
                            "output_hash": output_hash,
                            "latency_ms": latency_ms,
                            "trace_id": context.trace_id,
                            "retry_count": retry_count,
                        }
                    )
                    span.set_attribute("tool.status", final.status)
                    span.set_attribute("tool.latency_ms", latency_ms)
                    span.set_attribute("tool.retry_count", retry_count)
                    span.set_attribute("tool.output_hash", output_hash)
                    if final.error_code:
                        span.set_attribute("tool.error_code", final.error_code)
                    oi.set_io(span, output_value=final.output or {})
                    # 검색류 도구(output 에 chunks 리스트)는 RETRIEVER kind + 문서별 id/score
                    # 를 OpenInference 스키마로 단다(D5) — Phoenix 가 RETRIEVER span 에서
                    # 문서 목록·점수를 구조화 렌더한다(generic IO 만으론 안 보임). 다른 도구는
                    # 영향 없음(chunks 없으면 skip). _enrich_retrieval 이 방어적으로 처리.
                    _enrich_retrieval_span(span, final.output)
                    await self._record(context.interaction_id, final)
                    if final.status == "failed":
                        if spec.required:
                            raise RequiredToolFailed(
                                name,
                                ToolErrorCode(final.error_code or "tool_internal_error"),
                                final.error_message or "",
                            )
                    return final
                except asyncio.TimeoutError:
                    last_error = ToolTimeout(name, spec.timeout_ms)
                    if attempt + 1 < attempts:
                        continue
                except RequiredToolFailed:
                    raise
                except Exception as e:  # noqa: BLE001
                    last_error = ToolFailure(
                        name, ToolErrorCode.INTERNAL_ERROR, str(e)
                    )
                    if attempt + 1 < attempts:
                        continue

            assert last_error is not None
            latency_ms = int((time.monotonic() - started) * 1000)
            failed = ToolResult(
                tool_name=name,
                tool_version=spec.version,
                status="failed",
                error_code=last_error.code.value,
                error_message=last_error.message,
                latency_ms=latency_ms,
                input_hash=input_hash,
                output_hash=None,
                trace_id=context.trace_id,
                retry_count=retry_count,
            )
            span.set_attribute("tool.status", "failed")
            span.set_attribute("tool.latency_ms", latency_ms)
            span.set_attribute("tool.retry_count", retry_count)
            span.set_attribute("tool.error_code", last_error.code.value)
            await self._record(context.interaction_id, failed)

            if spec.required:
                raise RequiredToolFailed(name, last_error.code, last_error.message)
            return failed

    async def _record(self, interaction_id: str, result: ToolResult) -> None:
        await self._sink.write_tool_result_record(
            interaction_id,
            result.model_dump(mode="json"),
        )
