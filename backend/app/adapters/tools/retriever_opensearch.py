from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.adapters.tools._opensearch_hybrid import build_hybrid_query
from app.domain.errors import RetrievalTimeoutError, RetrievalUnavailableError
from app.domain.retrieval import (
    RetrievedChunk,
    RetrieverSearchInput,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.embedding import DenseEncoderPort, SparseEncoderPort
from app.ports.tool import ToolExecutionContext


class OpenSearchRetrieverTool:
    """Hybrid retriever (BM25 + dense kNN + sparse rank_features) against OpenSearch 3.x.

    The DSL is built by `build_hybrid_query` using injected encoders. Entity
    boosts and ``scenario_object`` filters live inside the BM25 sub-query of
    the hybrid clause; dense/sparse sub-queries stay scenario-agnostic.

    Document schema expected on the index (``nrc-all-v1``, NRC ADAMS/govinfo):
        {
          "chunk_id":         "ML15355A364_c0001",
          "source_id":        "ML15355A364",
          "collection":       "DSRS",                       # 10CFR|DSRS|FR|RG|SRP|nuscale_*
          "search_type":      "manual",                     # manual | nuscale
          "section_path":     ["...","..."],                # 계층 섹션
          "section_path_str": "... > ...",
          "page_start":       1,
          "page_end":         3,
          "text":             "<chunk body>",
          "dense_e5":         [float, ...]                  # knn_vector(1024)
          "sparse_fermi":     {tok: float}                  # rank_features
          "doc_metadata": {
            "AccessionNumber": "ML15355A364",               # ADAMS
            "DocumentTitle":   "...",
            "DocumentDate":    "YYYY-MM-DD",
            "dateIssued":      "YYYY-MM-DD",                # govinfo
            "title":           "...",                       # govinfo
            ...
          }
        }

    v3.1 규제 메타 (``clause_id`` / ``authority_tier`` / ``jurisdiction`` /
    ``effective_on``) 는 *예정* 스키마 ``nrc-all-v2`` 에서만 존재한다. 현행
    적재 데이터는 v1 이므로 이 어댑터는 해당 필드를 *있으면 읽고 없으면 None*
    으로 처리하며 (``authority_tier`` 만 v1 의 ``collection`` 에서 read-time
    유도), v1/v2 양쪽에서 동일하게 동작한다. 자세한 전환 배경은
    ``infra/opensearch/mappings/README.md`` 참고.
    """

    name = "retriever.search"
    version = "v2"

    def __init__(
        self,
        *,
        endpoint: str,
        index: str,
        dense_encoder: DenseEncoderPort,
        sparse_encoder: SparseEncoderPort,
        search_pipeline: str | None = None,
        dense_field: str = "dense_e5",
        sparse_field: str = "sparse_fermi",
        text_field: str = "text",
        k_dense: int = 50,
        username: str | None = None,
        password: str | None = None,
        timeout_s: float = 5.0,
        verify_certs: bool = False,
        strategy_pipelines: dict[str, str] | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("OpenSearchRetrieverTool requires endpoint")
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._dense = dense_encoder
        self._sparse = sparse_encoder
        self._search_pipeline = search_pipeline or None
        self._dense_field = dense_field
        self._sparse_field = sparse_field
        self._text_field = text_field
        self._k_dense = k_dense
        self._auth = (username, password) if username else None
        self._timeout_s = timeout_s
        self._verify = verify_certs
        # v3.1 Node 5 strategy → search_pipeline. "hybrid" 는 생성자 기본
        # pipeline(설정값) 사용. 나머지는 repo 의 weight-변종 pipeline 으로 매핑.
        self._strategy_pipelines: dict[str, str] = strategy_pipelines or {
            "bm25": "nrc-hybrid-bm25-only",
            "vector": "nrc-hybrid-dense-only",
            "dense": "nrc-hybrid-dense-only",
            "sparse": "nrc-hybrid-sparse-only",
        }

    def _pipeline_for_strategy(self, strategy: str | None) -> str | None:
        if strategy and strategy != "hybrid" and strategy in self._strategy_pipelines:
            return self._strategy_pipelines[strategy]
        return self._search_pipeline

    async def invoke(
        self,
        tool_input: RetrieverSearchInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)

        # build_hybrid_query 는 동기 함수이며 내부에서 E5(dense)·Fermi(sparse) torch
        # forward 를 동기로 실행한다. 이벤트 루프 스레드에서 직접 호출하면 인코딩 동안
        # 루프가 블록되고, gather 된 동시 검색이 인코드 시점에 직렬화된다. to_thread 로
        # 단 한 번 오프로드 — torch 가 C 연산 중 GIL 을 풀어 동시 검색의 인코딩이 풀스레드
        # 에서 실제로 겹친다. build_hybrid_query 는 순수 함수이고 인코더는 inference_mode
        # read-only 라 동시 호출에 안전. 시그니처 불변 → 모든 호출자(1차 검색 포함) 무영향.
        hq = await asyncio.to_thread(
            build_hybrid_query,
            tool_input,
            dense_encoder=self._dense,
            sparse_encoder=self._sparse,
            dense_field=self._dense_field,
            sparse_field=self._sparse_field,
            text_field=self._text_field,
            k_dense=self._k_dense,
        )
        url = f"{self._endpoint}/{self._index}/_search"
        params: dict[str, str] = {}
        # v3.1 Node 5: strategy → search_pipeline. 단일 전략 pipeline 은 동일한
        # 3-sub-query hybrid DSL 에 weight 변종을 적용한다 (bm25-only=[1,0,0],
        # dense-only=[0,1,0], hybrid=[.2,.3,.5]). DSL 은 그대로, pipeline 만 교체.
        # 미지의 strategy 는 생성자 기본 pipeline 으로 폴백.
        pipeline = self._pipeline_for_strategy(tool_input.strategy)
        if pipeline:
            params["search_pipeline"] = pipeline

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, verify=self._verify, auth=self._auth
            ) as client:
                resp = await client.post(
                    url,
                    json=hq.dsl,
                    params=params or None,
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code >= 500:
                raise RetrievalUnavailableError(
                    f"opensearch retriever {resp.status_code}: {resp.text[:200]}"
                )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise RetrievalTimeoutError(f"opensearch retriever timeout: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise RetrievalUnavailableError(
                f"opensearch retriever http error: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise RetrievalUnavailableError(
                f"opensearch retriever unreachable: {exc}"
            ) from exc

        hits = (data.get("hits") or {}).get("hits") or []
        chunks = [self._hit_to_chunk(hit) for hit in hits[: max(1, tool_input.top_k)]]
        output = RetrieverSearchOutput(chunks=chunks)

        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",
            trace_id=context.trace_id,
        )

    @staticmethod
    def _hit_to_chunk(hit: dict[str, Any]) -> RetrievedChunk:
        src = hit.get("_source", {}) or {}
        meta = src.get("doc_metadata") or {}
        text = src.get("text", "") or ""

        # source_id는 nrc-all-v1 의 1차 문서 식별자. 없으면 ADAMS AccessionNumber,
        # 그래도 없으면 _id 사용.
        source_id = src.get("source_id") or meta.get("AccessionNumber") or hit.get("_id") or "unknown"
        chunk_id = src.get("chunk_id") or hit.get("_id") or source_id

        # 섹션은 계층 배열을 " > " 로 합쳐 단일 문자열로 (이미 색인된 section_path_str 우선).
        section_path = src.get("section_path") or []
        section = src.get("section_path_str") or (" > ".join(section_path) if section_path else None)

        # 응답일자: ADAMS DocumentDate 우선, govinfo dateIssued 차순.
        response_date = meta.get("DocumentDate") or meta.get("dateIssued")
        title = meta.get("DocumentTitle") or meta.get("title")

        # v3.1 규제 메타 (Node 6 G3). 인덱스 _source 에 명시 필드가 있으면 그대로
        # 사용하고, 없으면 가능한 범위에서 유도 — 재인덱싱 이전에도 G3 가 동작하도록.
        collection = src.get("collection")
        authority_tier = src.get("authority_tier") or _derive_authority_tier(collection)
        # effective_on 은 *발효/개정 기준일*이며 문서 *제출일*(response_date)과
        # 의미가 다르다. 인덱스에 명시값이 없으면 None 으로 둔다 — response_date 로
        # bridge 하면 PR-5 version_match hard gate 가 제출일을 발효일로 오인해
        # 모든 chunk 에 대해 잘못된 버전 충돌을 계산하게 된다 (false answer ≫
        # false refusal 비대칭에 반함). unknown 은 unknown 으로 전달.
        # clause_id / jurisdiction 도 동일하게 추측하지 않는다.
        effective_on = src.get("effective_on")

        return RetrievedChunk(
            chunk_id=chunk_id,
            document_id=source_id,
            score=float(hit.get("_score", 0.0)),
            page=src.get("page_start"),
            section=section,
            snippet=text[:512],
            # full body 도 싣는다 — N3.5 follow_up 참조 추출은 본문 전체를 봐야
            # char 512 뒤에 나오는 인용(RG/NUREG/CFR…)을 잡는다(snippet 만으로는
            # 멀티홉이 사실상 0건). 생성 컨텍스트는 snippets 모드라 snippet 만 읽고
            # (context/pack.py render_for_prompt), to_snapshot 이 snippets 모드에서
            # text 를 blank 처리하므로 아티팩트·토큰 추정에도 영향 없다.
            text=text or None,
            doc_type=collection,
            revision=None,  # NRC 스키마에 대응 필드 없음
            response_date=response_date,
            collection=collection,
            search_type=src.get("search_type"),
            source_id=source_id if src.get("source_id") else None,
            page_end=src.get("page_end"),
            title=title,
            clause_id=src.get("clause_id"),
            authority_tier=authority_tier,
            jurisdiction=src.get("jurisdiction"),
            effective_on=effective_on,
            token_count=src.get("token_count"),
            section_path=list(section_path) if section_path else None,
        )


# NRC 코퍼스의 collection → authority_tier 유도표 (1차 법령 > 2차 가이드 > 3차 해설).
# 명시 `authority_tier` 필드가 인덱스에 들어오기 전까지의 read-time 기본값.
# 10CFR/FR = 연방규정·관보(법령) → primary; RG/DSRS/SRP = NRC 가이드 → secondary;
# nuscale_* = 벤더 제출문서 → tertiary. 미지 collection 은 None (hard gate 가 판단).
_AUTHORITY_TIER_BY_COLLECTION: dict[str, str] = {
    "10CFR": "primary",
    "FR": "primary",
    "RG": "secondary",
    "DSRS": "secondary",
    "SRP": "secondary",
}


def _derive_authority_tier(collection: str | None) -> str | None:
    if not collection:
        return None
    if collection in _AUTHORITY_TIER_BY_COLLECTION:
        return _AUTHORITY_TIER_BY_COLLECTION[collection]
    if collection.startswith("nuscale"):
        return "tertiary"
    return None
