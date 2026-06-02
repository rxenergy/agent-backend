from __future__ import annotations

import json

import httpx

from app.adapters.tools.document_local import LocalDocumentFetchSectionTool
from app.adapters.tools.document_opensearch import (
    OpenSearchDocumentFetchSectionTool,
    _chunk_ordinal,
)
from app.ports.tool import ToolExecutionContext


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="i", trace_id="t", app_profile="local",
        agent_variant="hierarchical_corrective_v3_1",
    )


def _patch_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(
        "app.adapters.tools.document_opensearch.httpx.AsyncClient", factory
    )


def test_chunk_ordinal_parses_trailing_int():
    assert _chunk_ordinal("ML1_c0003") == 3
    assert _chunk_ordinal("ML1_c0012") == 12
    # 사전식이면 깨질 자리(9999↑)도 정수로 올바른 순서.
    assert _chunk_ordinal("d_c10000") > _chunk_ordinal("d_c9999")
    assert _chunk_ordinal("noord") == 0


async def test_fetch_section_term_dsl(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        # 일부러 역순으로 줘서 어댑터의 ordinal 재정렬을 검증.
        return httpx.Response(200, json={"hits": {"hits": [
            {"_id": "c2", "_source": {"chunk_id": "S1_c0002", "source_id": "S1",
                                       "text": "second", "section_path": ["3.5", "3.5.1"]}},
            {"_id": "c1", "_source": {"chunk_id": "S1_c0001", "source_id": "S1",
                                       "text": "first", "section_path": ["3.5", "3.5.1"]}},
        ]}})

    _patch_client(monkeypatch, handler)
    tool = OpenSearchDocumentFetchSectionTool(endpoint="http://os:9200", index="nrc-all-v1")
    res = await tool.invoke(
        {"source_id": "S1", "section_key": "3.5.1 Sub", "max_chunks": 50}, _ctx()
    )
    q = captured["body"]["query"]["bool"]["filter"]
    assert {"term": {"source_id": "S1"}} in q
    assert {"term": {"section_path": "3.5.1 Sub"}} in q   # term(exact) by default
    assert captured["body"]["sort"] == [{"chunk_id": "asc"}]
    # ordinal 재정렬 → 본문 순서 복원.
    ids = [c["chunk_id"] for c in res.output["chunks"]]
    assert ids == ["S1_c0001", "S1_c0002"]


async def test_fetch_section_prefix_dsl_for_multihop(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, handler)
    tool = OpenSearchDocumentFetchSectionTool(endpoint="http://os:9200", index="nrc-all-v1")
    await tool.invoke(
        {"source_id": "S1", "section_key": "3.2", "max_chunks": 50, "match": "prefix"},
        _ctx(),
    )
    q = captured["body"]["query"]["bool"]["filter"]
    assert {"prefix": {"section_path": "3.2"}} in q   # prefix(번호) for §-ref
    assert {"term": {"source_id": "S1"}} in q


async def test_fetch_section_missing_keys_skips_query(monkeypatch):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, handler)
    tool = OpenSearchDocumentFetchSectionTool(endpoint="http://os:9200", index="nrc-all-v1")
    res = await tool.invoke({"source_id": "", "section_key": ""}, _ctx())
    assert res.output["chunks"] == []
    assert called["n"] == 0   # 키 없으면 OpenSearch 호출 안 함


async def test_local_fetch_section_returns_empty():
    res = await LocalDocumentFetchSectionTool().invoke(
        {"source_id": "S1", "section_key": "3.5.1", "max_chunks": 50}, _ctx()
    )
    assert res.status == "success"
    assert res.output["chunks"] == []   # graceful no-op (make smoke 무영향)
