from __future__ import annotations

import json

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


class _FakeDense:
    dim = 4

    def encode_query(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    def warmup(self) -> None:  # pragma: no cover
        pass


class _FakeSparse:
    def encode_query(self, text: str) -> dict[str, float]:
        return {"loca": 1.5}

    def warmup(self) -> None:  # pragma: no cover
        pass


def _retriever(**overrides) -> OpenSearchRetrieverTool:
    kwargs = dict(
        endpoint="http://os:9200",
        index="nrc-all-v3",
        dense_encoder=_FakeDense(),
        sparse_encoder=_FakeSparse(),
        search_pipeline="nrc-hybrid-search",
    )
    kwargs.update(overrides)
    return OpenSearchRetrieverTool(**kwargs)


async def test_retriever_maps_hits_to_chunks(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.read().decode("utf-8"))
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
    tool = _retriever()
    result = await tool.invoke(
        {"query_text": "LOCA 잔열 제거", "top_k": 2, "scenario_object": "regulation"},
        _ctx(),
    )
    assert result.status == "success"
    chunks = result.output["chunks"]
    assert len(chunks) == 2
    assert chunks[0]["chunk_id"] == "kins-1#p7#1"
    assert chunks[0]["score"] == pytest.approx(4.2)
    assert "/nrc-all-v3/_search" in captured["url"]
    assert "search_pipeline=nrc-hybrid-search" in captured["url"]
    # Hybrid DSL: three sub-queries; scenario_object filter on the BM25 sub-query
    subs = captured["body"]["query"]["hybrid"]["queries"]
    assert len(subs) == 3
    assert subs[0]["bool"]["filter"] == [{"term": {"scenario_object": "regulation"}}]
    assert subs[1]["knn"]["dense_e5"]["vector"] == [0.1, 0.2, 0.3, 0.4]
    assert subs[2]["bool"]["should"][0]["rank_feature"]["field"] == "sparse_fermi.loca"


async def test_retriever_empty_results(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()
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
    tool = OpenSearchDocumentResolverTool(endpoint="http://os:9200", index="nrc-all-v3")
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
        OpenSearchRetrieverTool(
            endpoint="",
            index="nrc-all-v3",
            dense_encoder=_FakeDense(),
            sparse_encoder=_FakeSparse(),
        )


async def test_retriever_maps_timeout_to_domain_error(monkeypatch):
    from app.domain.errors import RetrievalTimeoutError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()
    with pytest.raises(RetrievalTimeoutError):
        await tool.invoke({"query_text": "q", "top_k": 1}, _ctx())


async def test_retriever_maps_5xx_to_domain_error(monkeypatch):
    from app.domain.errors import RetrievalUnavailableError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()
    with pytest.raises(RetrievalUnavailableError):
        await tool.invoke({"query_text": "q", "top_k": 1}, _ctx())


async def test_document_resolver_maps_request_error(monkeypatch):
    from app.domain.errors import RetrievalUnavailableError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_client(monkeypatch, "app.adapters.tools.document_opensearch", handler)
    tool = OpenSearchDocumentResolverTool(endpoint="http://os:9200", index="nrc-all-v3")
    with pytest.raises(RetrievalUnavailableError):
        await tool.invoke(
            {"citation_ids": ["c1"], "chunk_ids": ["kins-1#p7#1"]}, _ctx()
        )
