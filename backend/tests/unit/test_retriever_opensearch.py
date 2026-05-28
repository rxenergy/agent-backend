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
        index="nrc-all-v1",
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
                                "chunk_id": "ML15355A364_c0001",
                                "source_id": "ML15355A364",
                                "collection": "DSRS",
                                "search_type": "manual",
                                "section_path": ["3.5 Missile Protection", "3.5.1.3 Turbine Missiles"],
                                "section_path_str": "3.5 Missile Protection > 3.5.1.3 Turbine Missiles",
                                "page_start": 7,
                                "page_end": 9,
                                "text": "Turbine missile protection requirements for SMR...",
                                "doc_metadata": {
                                    "AccessionNumber": "ML15355A364",
                                    "DocumentTitle": "NuScale DSRS 3.5.1.3 Turbine Missiles",
                                    "DocumentDate": "2016-07-22",
                                },
                            },
                        },
                        {
                            "_id": "h2",
                            "_score": 3.1,
                            "_source": {
                                "chunk_id": "ML15355A364_c0002",
                                "source_id": "ML15355A364",
                                "collection": "DSRS",
                                "search_type": "manual",
                                "section_path": ["3.5 Missile Protection"],
                                "page_start": 10,
                                "page_end": 12,
                                "text": "Additional design provisions...",
                                "doc_metadata": {
                                    "AccessionNumber": "ML15355A364",
                                    "DocumentTitle": "NuScale DSRS 3.5.1.3 Turbine Missiles",
                                },
                            },
                        },
                    ]
                }
            },
        )

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()
    result = await tool.invoke(
        {"query_text": "turbine missile protection", "top_k": 2, "scenario_object": "regulation"},
        _ctx(),
    )
    assert result.status == "success"
    chunks = result.output["chunks"]
    assert len(chunks) == 2
    # NRC 스키마: chunk_id 그대로, document_id 는 source_id 매핑
    assert chunks[0]["chunk_id"] == "ML15355A364_c0001"
    assert chunks[0]["document_id"] == "ML15355A364"
    assert chunks[0]["score"] == pytest.approx(4.2)
    assert chunks[0]["page"] == 7
    assert chunks[0]["page_end"] == 9
    assert chunks[0]["section"] == "3.5 Missile Protection > 3.5.1.3 Turbine Missiles"
    assert chunks[0]["collection"] == "DSRS"
    assert chunks[0]["search_type"] == "manual"
    assert chunks[0]["doc_type"] == "DSRS"  # NRC 도메인에서 collection 으로 매핑
    assert chunks[0]["response_date"] == "2016-07-22"
    assert chunks[0]["title"] == "NuScale DSRS 3.5.1.3 Turbine Missiles"

    assert "/nrc-all-v1/_search" in captured["url"]
    assert "search_pipeline=nrc-hybrid-search" in captured["url"]
    # Hybrid DSL: three sub-queries; scenario_object → search_type filter는 top-level hybrid.filter 로.
    hybrid = captured["body"]["query"]["hybrid"]
    subs = hybrid["queries"]
    assert len(subs) == 3
    # BM25 sub-query 는 더 이상 filter 를 자체로 갖지 않음 (top-level 로 이동)
    assert subs[0]["bool"].get("filter", []) == []
    assert subs[1]["knn"]["dense_e5"]["vector"] == [0.1, 0.2, 0.3, 0.4]
    # sparse rank_features (encode_query → {"turbine": 1.5}) 첫 토큰 확인
    assert subs[2]["bool"]["should"][0]["rank_feature"]["field"].startswith("sparse_fermi.")
    # 공통 필터는 search_type=manual (regulation → manual 매핑)
    assert hybrid["filter"] == {
        "bool": {"filter": [{"term": {"search_type": "manual"}}]}
    }


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
                                "chunk_id": "ML15355A364_c0001",
                                "source_id": "ML15355A364",
                                "page_start": 7,
                                "section_path_str": "3.5.1.3 Turbine Missiles",
                            }
                        }
                    ]
                }
            },
        )

    _patch_client(monkeypatch, "app.adapters.tools.document_opensearch", handler)
    tool = OpenSearchDocumentResolverTool(endpoint="http://os:9200", index="nrc-all-v1")
    result = await tool.invoke(
        {
            "citation_ids": ["c1", "c2"],
            "chunk_ids": ["ML15355A364_c0001", "missing-chunk"],
        },
        _ctx(),
    )
    resolved = result.output["resolved"]
    assert resolved[0]["resolvable"] is True
    # document resolver 도 NRC 스키마(source_id, page_start)로 갱신 필요할 수 있음
    # 다만 본 테스트는 단지 resolvable 플래그만 검증, 세부 필드는 resolver 코드에서 처리
    assert resolved[1]["resolvable"] is False
    assert resolved[1]["document_id"] is None


def test_retriever_endpoint_required():
    with pytest.raises(ValueError):
        OpenSearchRetrieverTool(
            endpoint="",
            index="nrc-all-v1",
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
    tool = OpenSearchDocumentResolverTool(endpoint="http://os:9200", index="nrc-all-v1")
    with pytest.raises(RetrievalUnavailableError):
        await tool.invoke(
            {"citation_ids": ["c1"], "chunk_ids": ["kins-1#p7#1"]}, _ctx()
        )
