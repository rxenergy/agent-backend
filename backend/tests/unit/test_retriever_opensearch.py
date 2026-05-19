from __future__ import annotations

import httpx
import pytest

from app.adapters.tools.document_opensearch import OpenSearchDocumentResolverTool
from app.adapters.tools.retriever_opensearch import OpenSearchRetrieverTool
from app.ports.tool import ToolExecutionContext


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="inter-1",
        trace_id="trace-1",
        app_profile="local",
        agent_variant="sequential_tool_routed_v2",
    )


def _patch_client(monkeypatch, module: str, handler):
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(f"{module}.httpx.AsyncClient", factory)


async def test_retriever_maps_hits_to_chunks(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        body = request.read().decode("utf-8")
        captured["body"] = body
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_id": "h1",
                            "_score": 4.2,
                            "_source": {
                                "chunk_id": "kins-1#p7#1",
                                "document_id": "kins-1",
                                "page": 7,
                                "section": "§3.2",
                                "text": "냉각재 상실 사고 시 잔열 제거에 관한 규정...",
                            },
                        },
                        {
                            "_id": "h2",
                            "_score": 3.1,
                            "_source": {
                                "chunk_id": "kins-1#p8#1",
                                "document_id": "kins-1",
                                "page": 8,
                                "section": "§3.3",
                                "text": "비상노심냉각계통(ECCS)...",
                            },
                        },
                    ]
                }
            },
        )

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = OpenSearchRetrieverTool(endpoint="http://os:9200", index="smr-docs")
    result = await tool.invoke(
        {"query_text": "LOCA 잔열 제거", "top_k": 2, "scenario_object": "regulation"}, _ctx()
    )
    assert result.status == "success"
    chunks = result.output["chunks"]
    assert len(chunks) == 2
    assert chunks[0]["chunk_id"] == "kins-1#p7#1"
    assert chunks[0]["document_id"] == "kins-1"
    assert chunks[0]["page"] == 7
    assert chunks[0]["score"] == pytest.approx(4.2)
    assert "/smr-docs/_search" in captured["url"]
    assert "scenario_object" in captured["body"]


async def test_retriever_empty_results(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = OpenSearchRetrieverTool(endpoint="http://os:9200", index="smr-docs")
    result = await tool.invoke({"query_text": "no match", "top_k": 3}, _ctx())
    assert result.status == "success"
    assert result.output["chunks"] == []


async def test_document_resolver_marks_missing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "chunk_id": "kins-1#p7#1",
                                "document_id": "kins-1",
                                "page": 7,
                                "section": "§3.2",
                            }
                        }
                    ]
                }
            },
        )

    _patch_client(monkeypatch, "app.adapters.tools.document_opensearch", handler)
    tool = OpenSearchDocumentResolverTool(endpoint="http://os:9200", index="smr-docs")
    result = await tool.invoke(
        {
            "citation_ids": ["c1", "c2"],
            "chunk_ids": ["kins-1#p7#1", "missing-chunk"],
        },
        _ctx(),
    )
    resolved = result.output["resolved"]
    assert resolved[0]["resolvable"] is True
    assert resolved[0]["document_id"] == "kins-1"
    assert resolved[0]["page"] == 7
    assert resolved[1]["resolvable"] is False
    assert resolved[1]["document_id"] is None


def test_retriever_endpoint_required():
    with pytest.raises(ValueError):
        OpenSearchRetrieverTool(endpoint="", index="smr-docs")
