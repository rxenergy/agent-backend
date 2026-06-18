"""document.fetch_chunks + compute_neighbor_ids 단위 테스트.

verify_slot 의 neighbor_requests(앞/뒤 문맥 보강) → 코드가 이웃 chunk_id 를 ordinal 로
계산(compute_neighbor_ids) → document.fetch_chunks 가 그 id 집합을 terms 쿼리로 일괄 fetch
하는 경로를 컨테이너 없이 검증한다(fake httpx transport + local fake 빈 결과).
"""

from __future__ import annotations

import json

import httpx

from app.adapters.tools.document_local import LocalDocumentFetchChunksTool
from app.adapters.tools.document_opensearch import OpenSearchDocumentFetchChunksTool
from app.application.agents.slot_pipeline import compute_neighbor_ids
from app.ports.tool import ToolExecutionContext


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="i", trace_id="t", app_profile="local",
        agent_variant="spec_driven_v2",
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


# --- compute_neighbor_ids ---------------------------------------------------

def test_neighbor_ids_before_after_both():
    out = compute_neighbor_ids({
        "S1_c0005": "before",   # → c0004
        "S1_c0008": "after",    # → c0009
        "S1_c0010": "both",     # → c0009, c0011
    })
    assert "S1_c0004" in out
    assert "S1_c0009" in out
    assert "S1_c0011" in out
    # both 가 만든 c0009 와 after 가 만든 c0009 는 한 번만(중복 제거).
    assert out.count("S1_c0009") == 1


def test_neighbor_ids_zero_floor_and_width_growth():
    # ord 0 의 before(=-1)는 제외. 9999 의 after 는 자릿수 확장(10000).
    out = compute_neighbor_ids({"S1_c0000": "both", "S1_c9999": "after"})
    assert "S1_c0001" in out          # after of 0000
    assert not any(n.endswith("c-1") for n in out)  # before of 0000 제외
    assert "S1_c10000" in out         # 9999 → 10000 (자릿수 자연 확장)


def test_neighbor_ids_skips_non_ordinal_chunk():
    # `_cNNNN` 형식이 아니면 이웃 계산 불가 → skip.
    assert compute_neighbor_ids({"weird-id": "after"}) == []
    assert compute_neighbor_ids({"S1_cABC": "before"}) == []


# --- OpenSearch fetch_chunks ------------------------------------------------

async def test_fetch_chunks_terms_dsl_and_reorder(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        # 역순으로 줘서 ordinal 재정렬을 검증.
        return httpx.Response(200, json={"hits": {"hits": [
            {"_id": "c9", "_source": {"chunk_id": "S1_c0009", "source_id": "S1",
                                       "text": "after"}},
            {"_id": "c4", "_source": {"chunk_id": "S1_c0004", "source_id": "S1",
                                       "text": "before"}},
        ]}})

    _patch_client(monkeypatch, handler)
    tool = OpenSearchDocumentFetchChunksTool(endpoint="http://os:9200", index="nrc-all-v1")
    res = await tool.invoke({"chunk_ids": ["S1_c0009", "S1_c0004"]}, _ctx())

    # terms 쿼리(중복/빈 제거 후 정렬된 unique 집합).
    assert captured["body"]["query"] == {"terms": {"chunk_id": ["S1_c0004", "S1_c0009"]}}
    # ordinal 재정렬 → 본문 순서 복원.
    ids = [c["chunk_id"] for c in res.output["chunks"]]
    assert ids == ["S1_c0004", "S1_c0009"]


async def test_fetch_chunks_empty_skips_query(monkeypatch):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, handler)
    tool = OpenSearchDocumentFetchChunksTool(endpoint="http://os:9200", index="nrc-all-v1")
    res = await tool.invoke({"chunk_ids": []}, _ctx())
    assert res.output["chunks"] == []
    assert called["n"] == 0   # id 없으면 OpenSearch 호출 안 함


async def test_local_fetch_chunks_returns_empty():
    res = await LocalDocumentFetchChunksTool().invoke(
        {"chunk_ids": ["S1_c0004"]}, _ctx()
    )
    assert res.status == "success"
    assert res.output["chunks"] == []   # graceful no-op (make smoke 무영향)
