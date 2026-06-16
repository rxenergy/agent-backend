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
        agent_variant="agentic_finder_v4",
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
                                # v3.1 규제 메타가 인덱스에 명시된 hit (v2 스키마로
                                # 재적재된 문서를 시뮬레이션 — v1 데이터엔 부재).
                                "clause_id": "DSRS_3_5_1_3",
                                "authority_tier": "secondary",
                                "jurisdiction": "NRC",
                                "effective_on": "2016-07-22",
                                "section_path": ["3.5 Missile Protection", "3.5.1.3 Turbine Missiles"],
                                "section_path_str": "3.5 Missile Protection > 3.5.1.3 Turbine Missiles",
                                "page_start": 7,
                                "page_end": 9,
                                "text": "Turbine missile protection requirements for SMR... [TABLE: tb_0001]",
                                # 본문에서 분리된 표(array) — full 모드 render 가 매칭 tag 의
                                # caption+markdown 으로 마커를 인라인 치환한다.
                                "tables": [{"tag": "tb_0001", "caption": "Tbl",
                                            "markdown": "| 항목 | 값 |", "html": ""}],
                                "doc_metadata": {
                                    "AccessionNumber": "ML15355A364",
                                    "DocumentTitle": "NuScale DSRS 3.5.1.3 Turbine Missiles",
                                    "DocumentDate": "2016-07-22",
                                    # 원문 다운로드 URL(ADAMS Url) — References 딥링크 1차 소스.
                                    "Url": "https://www.nrc.gov/docs/ML1535/ML15355A364.pdf",
                                    # 검색 스코프 표준 메타(search_scope_metadata §6.6).
                                    "std_status": "current",
                                    "std_canonical_id": "DSRS-3.5.1.3",
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
                                    "DocumentDate": "2016-07-22",
                                    # Url 부재 → download_pdfLink(govinfo PDF) fallback.
                                    "download_pdfLink": "https://www.govinfo.gov/content/"
                                                        "pkg/CFR-2024-title10-vol1/pdf/"
                                                        "CFR-2024-title10-vol1-sec50-46.pdf",
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
    # v3.1 regulatory meta — hit 1 has them explicit in _source.
    assert chunks[0]["clause_id"] == "DSRS_3_5_1_3"
    assert chunks[0]["authority_tier"] == "secondary"
    assert chunks[0]["jurisdiction"] == "NRC"
    assert chunks[0]["effective_on"] == "2016-07-22"
    # hit 2 has no explicit regulatory meta: authority_tier derived from
    # collection (DSRS → secondary); clause_id/jurisdiction not guessed.
    # effective_on stays None even though DocumentDate(=response_date) is
    # present — a filing date is NOT an effective date, so no proxy is made
    # (PR-5 version_match must see unknown as unknown).
    assert chunks[1]["authority_tier"] == "secondary"
    assert chunks[1]["clause_id"] is None
    assert chunks[1]["jurisdiction"] is None
    assert chunks[1]["response_date"] == "2016-07-22"
    assert chunks[1]["effective_on"] is None

    # text 전문(캡 없음) + snippet(캡) 둘 다 적재. tables 는 _source 원본 그대로
    # 싣고, tables 없는 hit 은 None (spec_driven_table_inline_expansion).
    assert chunks[0]["text"] == "Turbine missile protection requirements for SMR... [TABLE: tb_0001]"
    assert chunks[0]["snippet"].startswith("Turbine missile protection")
    assert chunks[0]["tables"] == [{"tag": "tb_0001", "caption": "Tbl",
                                    "markdown": "| 항목 | 값 |", "html": ""}]
    assert chunks[1]["tables"] is None

    # 검색 스코프 표준 메타(doc_metadata.std_*) — hit 1 보유, hit 2 부재 → None.
    assert chunks[0]["std_status"] == "current"
    assert chunks[0]["std_canonical_id"] == "DSRS-3.5.1.3"
    assert chunks[0]["std_design"] is None  # DSRS 는 규제라 design 빈값

    # 원문 다운로드 URL — hit 1 은 doc_metadata.Url(ADAMS), hit 2 는 Url 부재로
    # download_pdfLink(govinfo) fallback. References 딥링크 1차 소스.
    assert chunks[0]["source_url"] == "https://www.nrc.gov/docs/ML1535/ML15355A364.pdf"
    assert chunks[1]["source_url"] == (
        "https://www.govinfo.gov/content/pkg/CFR-2024-title10-vol1/pdf/"
        "CFR-2024-title10-vol1-sec50-46.pdf"
    )
    assert chunks[1]["std_status"] is None
    assert chunks[1]["std_canonical_id"] is None

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


async def test_retriever_scope_filters_and_noise_floor_in_dsl(monkeypatch):
    """v3.1 Layer 1/2: filters→term/terms, min_token_count→range(token_count),
    target→boosted terms in BM25 should. hybrid.queries 는 여전히 3개여야
    search_pipeline 의 3-weight 와 sync 가 유지된다(plan 결정 #1)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()
    await tool.invoke(
        {
            "query_text": "ECCS acceptance criteria",
            "top_k": 3,
            "filters": {"collection": ["10CFR", "RG"], "search_type": "manual"},
            "target": {"collection": ["SRP"]},
            "min_token_count": 12,
        },
        _ctx(),
    )
    hybrid = captured["body"]["query"]["hybrid"]
    # 4번째 sub-query 를 더하지 않는다 — 파이프라인 weight 배열과 desync 방지.
    assert len(hybrid["queries"]) == 3
    fclauses = hybrid["filter"]["bool"]["filter"]
    assert {"terms": {"collection": ["10CFR", "RG"]}} in fclauses
    assert {"term": {"search_type": "manual"}} in fclauses
    assert {"range": {"token_count": {"gte": 12}}} in fclauses
    # boost-scope(target)는 BM25 should 안의 terms-boost 로만 들어간다.
    bm25_should = hybrid["queries"][0]["bool"]["should"]
    assert any(
        "terms" in cl and cl["terms"].get("collection") == ["SRP"]
        and cl["terms"].get("boost")
        for cl in bm25_should
    )


async def test_retriever_wildcard_filter_for_fsar_canonical(monkeypatch):
    """spec_driven FSAR canonical: 값에 `*` 가 있으면 term/terms 가 아니라 wildcard
    절로 변환된다(FSAR-Part02*Ch06 → -T2- 유무·하위 Section 흡수). exact 값은 종전대로
    term/terms 유지."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()
    await tool.invoke(
        {
            "query_text": "ECCS passive emergency core cooling",
            "top_k": 5,
            "filters": {
                "noise": False,
                "collection": ["nuscale_FSAR"],
                "doc_metadata.std_canonical_id.keyword": ["FSAR-Part02*Ch06"],
            },
        },
        _ctx(),
    )
    fclauses = captured["body"]["query"]["hybrid"]["filter"]["bool"]["filter"]
    # wildcard 값 → wildcard 절.
    assert {"wildcard": {"doc_metadata.std_canonical_id.keyword": "FSAR-Part02*Ch06"}} in fclauses
    # exact 값(collection)은 종전대로 terms.
    assert {"terms": {"collection": ["nuscale_FSAR"]}} in fclauses
    assert {"term": {"noise": False}} in fclauses


async def test_retriever_no_scope_keeps_dsl_unchanged(monkeypatch):
    """빈 scope → filter 절 미생성(기존 동작 보존, sequential_v2 무영향)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()
    await tool.invoke({"query_text": "q", "top_k": 3}, _ctx())
    hybrid = captured["body"]["query"]["hybrid"]
    assert len(hybrid["queries"]) == 3
    assert "filter" not in hybrid  # scenario_object 도 없으니 filter 절 자체가 없음


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


@pytest.mark.parametrize(
    "strategy,expected_pipeline",
    [
        ("hybrid", "nrc-hybrid-search"),       # default constructor pipeline
        ("bm25", "nrc-hybrid-bm25-only"),
        ("vector", "nrc-hybrid-dense-only"),
        ("dense", "nrc-hybrid-dense-only"),
        ("sparse", "nrc-hybrid-sparse-only"),
        ("unknown-xyz", "nrc-hybrid-search"),  # fallback to default
    ],
)
async def test_strategy_selects_search_pipeline(monkeypatch, strategy, expected_pipeline):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"hits": {"hits": []}})

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()  # search_pipeline="nrc-hybrid-search"
    await tool.invoke({"query_text": "q", "top_k": 1, "strategy": strategy}, _ctx())
    assert f"search_pipeline={expected_pipeline}" in captured["url"]


@pytest.mark.parametrize(
    "collection,expected",
    [
        ("10CFR", "primary"),
        ("FR", "primary"),
        ("RG", "secondary"),
        ("DSRS", "secondary"),
        ("SRP", "secondary"),
        ("nuscale_dcd", "tertiary"),
        ("nuscale", "tertiary"),
        ("UNKNOWN", None),
        (None, None),
    ],
)
def test_derive_authority_tier(collection, expected):
    from app.adapters.tools.retriever_opensearch import _derive_authority_tier

    assert _derive_authority_tier(collection) == expected


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


async def test_full_text_loaded_uncapped_while_snippet_capped(monkeypatch):
    # D5 — snippet 은 캡(snippet_chars)되지만 text 는 전문이 잘림 없이 실린다.
    # 표 마커가 캡 뒤에 있어도 full 모드 render 가 치환할 수 있어야 하기 때문.
    long_text = "A" * 100 + " [TABLE: tb_0001]"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"hits": {"hits": [{
                "_id": "h1", "_score": 1.0,
                "_source": {
                    "chunk_id": "c1", "source_id": "s1", "collection": "RG",
                    "text": long_text,
                    "tables": [{"tag": "tb_0001", "markdown": "TBL"}],
                },
            }]}},
        )

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever(snippet_chars=10)  # 마커가 snippet 캡 밖
    result = await tool.invoke({"query_text": "q", "top_k": 1}, _ctx())
    chunk = result.output["chunks"][0]
    assert chunk["text"] == long_text  # 전문(캡 없음) — 마커 포함
    assert len(chunk["snippet"]) == 10  # snippet 은 캡됨
    assert "[TABLE:" not in chunk["snippet"]  # 마커가 캡에 잘림(text 에만 남음)


async def test_malformed_source_does_not_zero_out_search(monkeypatch):
    # 회귀: 색인 _source 가 모델 계약에 안 맞는 hit(tables 가 list 아님, text=비문자열)이
    # 섞여 있어도 검색이 통째로 0건이 되면 안 된다. 깨진 필드만 정규화하고 정상 hit 은
    # 변환한다(tables/text 방어적 정규화 + hit 단위 격리).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"hits": {"hits": [
                # 깨진 hit — tables 가 list 가 아님(dict, table 미분리 구 스키마).
                {"_id": "bad1", "_score": 2.0, "_source": {
                    "chunk_id": "bad1", "source_id": "s0", "collection": "nuscale_FSAR",
                    "text": "body", "tables": {"tb_1": {"text": "t"}}}},
                # 깨진 hit — text 가 문자열이 아님(list).
                {"_id": "bad2", "_score": 1.5, "_source": {
                    "chunk_id": "bad2", "source_id": "s0", "collection": "nuscale_FSAR",
                    "text": ["a", "b"], "tables": None}},
                # 정상 hit — tables 가 list[dict].
                {"_id": "ok1", "_score": 1.0, "_source": {
                    "chunk_id": "ok1", "source_id": "s1", "collection": "nuscale_FSAR",
                    "text": "good body",
                    "tables": [{"tag": "tb_1", "markdown": "TBL"}]}},
            ]}},
        )

    _patch_client(monkeypatch, "app.adapters.tools.retriever_opensearch", handler)
    tool = _retriever()
    result = await tool.invoke({"query_text": "q", "top_k": 10}, _ctx())
    assert result.status == "success"
    chunks = result.output["chunks"]
    # 깨진 hit 들은 정규화되어 변환되거나(text→"", tables→None) 최소한 검색을 죽이지
    # 않는다 — 정상 hit 은 반드시 보존된다.
    ids = {c["chunk_id"] for c in chunks}
    assert "ok1" in ids
    ok = next(c for c in chunks if c["chunk_id"] == "ok1")
    assert ok["text"] == "good body"
    assert ok["tables"] == [{"tag": "tb_1", "markdown": "TBL"}]
    # 깨진 hit(tables 가 dict)은 None 으로 정규화.
    bad1 = next(c for c in chunks if c["chunk_id"] == "bad1")
    assert bad1["tables"] is None
    # 정규화된 깨진 hit 의 타입 계약 확인.
    for c in chunks:
        assert isinstance(c["text"], str)
        assert c["tables"] is None or isinstance(c["tables"], list)
