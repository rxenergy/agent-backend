"""ToolExecutor — 검색류 도구 span 의 RETRIEVER enrich(D5).

검색 output({"chunks": [...]})이 OpenInference RETRIEVER 스키마(문서별 id/score)로 span 에
실리는지 확인한다. 본문(content)은 싣지 않는다(C1 — 거대 본문 회피). 비검색 도구는 영향 없음.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.application.tool_runtime.executor import ToolExecutor
from app.application.tool_runtime.registry import ToolRegistry
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


@pytest.fixture(scope="module")
def span_exporter():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


class _SearchTool:
    name = "retrieval.search"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        return ToolResult(
            tool_name="retrieval.search", tool_version="v1", status="success",
            output={"chunks": [
                {"chunk_id": "c1", "document_id": "D1", "score": 0.9,
                 "source_id": "S1", "snippet": "x" * 5000},
                {"chunk_id": "c2", "document_id": "D2", "score": 0.7,
                 "source_id": "S2", "snippet": "y" * 5000},
            ]},
            latency_ms=0, input_hash="x", trace_id="",
        )


class _PlainTool:
    name = "memory.read"
    version = "v1"

    async def invoke(self, tool_input, context) -> ToolResult:
        return ToolResult(
            tool_name="memory.read", tool_version="v1", status="success",
            output={"items": [1, 2, 3]}, latency_ms=0, input_hash="x", trace_id="",
        )


def _executor(tmp: Path, tools: dict[str, Any]) -> ToolExecutor:
    specs = {n: {"version": "v1", "adapter": "fake", "timeout_ms": 6000,
                 "retry": 0, "required": False} for n in tools}
    p = tmp / "reg.yaml"
    p.write_text(yaml.safe_dump({"tools": specs}))
    registry = ToolRegistry.from_yaml(p)
    sink = FilesystemEventSink(root=str(tmp / "events"), prefix="t")
    return ToolExecutor(registry=registry, tools=tools, event_sink=sink)


def _span(exporter, name_suffix: str):
    for sp in reversed(exporter.get_finished_spans()):
        if sp.name.endswith(name_suffix):
            return sp
    return None


@pytest.mark.asyncio
async def test_retrieval_span_carries_documents_and_kind(span_exporter) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ex = _executor(Path(tmp), {"retrieval.search": _SearchTool()})
        ctx = ToolExecutionContext(interaction_id="i1", trace_id="",
                                   app_profile="local", agent_variant="v")
        await ex.invoke("retrieval.search", {"query_text": "q"}, ctx)

    sp = _span(span_exporter, "retrieval.search")
    assert sp is not None
    attrs = dict(sp.attributes)
    # RETRIEVER kind 로 승격(generic TOOL 아님).
    assert attrs["openinference.span.kind"] == "RETRIEVER"
    # 문서별 id/score 가 OpenInference 스키마로.
    assert attrs["retrieval.documents.0.document.id"] == "c1"
    assert attrs["retrieval.documents.0.document.score"] == pytest.approx(0.9)
    assert attrs["retrieval.documents.1.document.id"] == "c2"
    assert attrs["retrieval.num_chunks"] == 2
    # 본문(snippet/content)은 span 에 실리지 않는다(C1 — 거대 본문 회피).
    assert "retrieval.documents.0.document.content" not in attrs


@pytest.mark.asyncio
async def test_non_retrieval_tool_stays_tool_kind(span_exporter) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ex = _executor(Path(tmp), {"memory.read": _PlainTool()})
        ctx = ToolExecutionContext(interaction_id="i2", trace_id="",
                                   app_profile="local", agent_variant="v")
        await ex.invoke("memory.read", {}, ctx)

    sp = _span(span_exporter, "memory.read")
    assert sp is not None
    attrs = dict(sp.attributes)
    # chunks 없음 → 일반 TOOL kind 유지, retrieval 속성 없음.
    assert attrs["openinference.span.kind"] == "TOOL"
    assert "retrieval.num_chunks" not in attrs
