from __future__ import annotations

import asyncio
import tempfile
from typing import Any

import pytest
from pydantic import BaseModel

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.application.tool_runtime.errors import RequiredToolFailed
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry, ToolSpec
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class _In(BaseModel):
    n: int


class SuccessTool:
    name = "t.success"
    version = "v1"

    async def invoke(self, tool_input: _In | dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"echo": tool_input.n if isinstance(tool_input, _In) else tool_input["n"]},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )


class FlakyTool:
    name = "t.flaky"
    version = "v1"

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, tool_input, context):
        self.calls += 1
        if self.calls < 2:
            raise RuntimeError("boom")
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output={"calls": self.calls},
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )


class SlowTool:
    name = "t.slow"
    version = "v1"

    async def invoke(self, tool_input, context):
        await asyncio.sleep(1.0)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )


def _registry(spec: ToolSpec) -> ToolRegistry:
    return ToolRegistry(specs={spec.name: spec})


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="i1",
        trace_id="",
        app_profile="local",
        agent_variant="test",
    )


@pytest.mark.asyncio
async def test_success_path_hashes_input_output() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sink = FilesystemEventSink(root=tmp, prefix="t")
        tool = SuccessTool()
        spec = ToolSpec(
            name=tool.name, version="v1", adapter="local",
            timeout_ms=1000, retry=0, required=False,
        )
        ex = ToolExecutor(registry=_registry(spec), tools={tool.name: tool}, event_sink=sink)

        r1 = await ex.invoke(tool.name, _In(n=42), _ctx())
        r2 = await ex.invoke(tool.name, _In(n=42), _ctx())
        assert r1.status == "success"
        assert r1.input_hash == r2.input_hash  # deterministic
        assert r1.output_hash == r2.output_hash
        assert r1.retry_count == 0


@pytest.mark.asyncio
async def test_retry_succeeds_within_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sink = FilesystemEventSink(root=tmp, prefix="t")
        tool = FlakyTool()
        spec = ToolSpec(
            name=tool.name, version="v1", adapter="local",
            timeout_ms=1000, retry=1, required=False,
        )
        ex = ToolExecutor(registry=_registry(spec), tools={tool.name: tool}, event_sink=sink)
        result = await ex.invoke(tool.name, {"n": 1}, _ctx())
        assert result.status == "success"
        assert result.retry_count == 1
        assert tool.calls == 2


@pytest.mark.asyncio
async def test_timeout_failed_optional() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sink = FilesystemEventSink(root=tmp, prefix="t")
        tool = SlowTool()
        spec = ToolSpec(
            name=tool.name, version="v1", adapter="local",
            timeout_ms=50, retry=0, required=False,
        )
        ex = ToolExecutor(registry=_registry(spec), tools={tool.name: tool}, event_sink=sink)
        result = await ex.invoke(tool.name, {}, _ctx())
        assert result.status == "failed"
        assert result.error_code == "tool_timeout"


@pytest.mark.asyncio
async def test_required_failure_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sink = FilesystemEventSink(root=tmp, prefix="t")
        tool = SlowTool()
        spec = ToolSpec(
            name=tool.name, version="v1", adapter="local",
            timeout_ms=50, retry=0, required=True,
        )
        ex = ToolExecutor(registry=_registry(spec), tools={tool.name: tool}, event_sink=sink)
        with pytest.raises(RequiredToolFailed):
            await ex.invoke(tool.name, {}, _ctx())
