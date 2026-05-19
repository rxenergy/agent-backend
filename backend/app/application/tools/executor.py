from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel

from app.application.tools.errors import (
    RequiredToolFailed,
    ToolFailure,
    ToolTimeout,
    ToolUnknown,
)
from app.application.tools.registry import ToolRegistry, ToolSpec
from app.domain.tools import ToolErrorCode, ToolResult, ToolStatus
from app.observability.otel import get_tracer
from app.ports.event_sink import EventSinkPort
from app.ports.tool import Tool, ToolExecutionContext

_TRACER = get_tracer("tool")


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
