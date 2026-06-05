from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.domain.retrieval import (
    RerankOutput,
    RetrievalPlan,
    RetrievalStrategy,
    RetrievedChunk,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext

# v3.1 Node 5 — retrieval_execute. 코드가 전략별로 retriever.search 를 병렬
# 호출(asyncio.gather)해 후보 풀을 만들고, cross-encoder reranker(retriever.rerank)
# 가 그 풀을 재정렬한다. LLM 미사용. (이전 RRF 융합은 제거 — reranker 가 순위 권위.)
#
# 재정렬 후 계약(downstream 가 반드시 지켜야 함):
#   • `ranked_chunks` 의 *순서*가 권위(authoritative) — reranker rank 다.
#   • `RetrievedChunk.score` 는 raw retriever 점수이며 rerank 순서와 일치하지
#     않는다(non-monotonic). 다운스트림은 *위치* 또는 `rerank_scores` 로 순위를
#     매기고, `chunk.score` 로 순위를 매기지 않는다(RRF 시절 계약 그대로 보존).
#   • raw `min_score` 필터는 *rerank 전, 전략별 raw 리스트*에 적용한다 — rerank
#     결과를 raw 로 거르면 reranker 가 올린 상위를 raw 가 낮다는 이유로 떨어뜨려
#     방금 한 재정렬과 충돌한다.
#   • reranker 가 실패/미배선이면(required:false) 1차 검색(union) 순서로 graceful
#     degrade 하고 `rerank_scores` 는 빈 dict (Node 6 G2 semantic 0.0 → lexical/
#     regulatory 게이팅). 실패는 record 되어 표면화된다(CLAUDE.md §6).


@dataclass
class DispatchResult:
    ranked_chunks: list[RetrievedChunk]  # reranker 순위(권위) — 비면 union 순서로 degrade
    tool_results: list[ToolResult] = field(default_factory=list)  # 성공 호출(record 용, rerank 포함)
    executed: list[RetrievalStrategy] = field(default_factory=list)  # 성공 전략 메타
    failed_strategies: list[str] = field(default_factory=list)
    rerank_scores: dict[str, float] = field(default_factory=dict)  # chunk_id → rerank 점수


class RetrievalDispatcher:
    """RetrievalPlan 의 전략들을 fan-out 실행해 후보 풀(중복 제거 union)을 만들고
    cross-encoder reranker 로 재정렬한다.

    부분 실패 내성: 일부 전략이 실패(RequiredToolFailed 등 예외)해도 ≥1 전략이
    성공하면 성공분만 푼다(다전략 redundancy 의 목적). 전부 실패하면 첫 예외를
    re-raise → conductor 가 RETRIEVAL_NO_RESULT refusal 로 매핑."""

    def __init__(self, tool_executor) -> None:
        self._tools = tool_executor

    async def execute(
        self,
        plan: RetrievalPlan,
        *,
        query_text: str,
        fetch_k: int,
        scenario_object: str | None,
        scenario_depth: str | None,
        entities: dict[str, list[str]],
        ctx: ToolExecutionContext,
        min_score: float = 0.0,
        top_k: int | None = None,
        target: dict[str, list[str]] | None = None,
        filters: dict[str, Any] | None = None,
        min_token_count: int = 0,
    ) -> DispatchResult:
        """전략별로 `fetch_k` 깊이로 검색 → raw min_score 전략별 필터 → union(dedup)
        → reranker 재정렬.

        `fetch_k` 는 *전략별 후보 풀 깊이*(spec Node 5 ~20). 최종 다운스트림 개수와
        분리한다 — 깊게 가져와 rerank 해야 reranker 가 더 나은 상위를 고른다. `top_k`
        가 주어지면 rerank 결과를 그 길이로 자르지만(상한), 보통 conductor 가 자른다."""
        strategies = [s.name for s in plan.strategies] or ["hybrid"]

        async def _one(strategy: str) -> ToolResult:
            return await self._tools.invoke(
                "retriever.search",
                {
                    "query_text": query_text,
                    "top_k": fetch_k,
                    "scenario_object": scenario_object,
                    "scenario_depth": scenario_depth,
                    "entities": entities,
                    "strategy": strategy,
                    # v3.1 범위·노이즈(Layer 1/2) — 전 strategy leg 공통.
                    "target": target or {},
                    "filters": filters or {},
                    "min_token_count": min_token_count,
                },
                ctx,
            )

        results = await asyncio.gather(
            *(_one(s) for s in strategies), return_exceptions=True
        )

        tool_results: list[ToolResult] = []
        executed: list[RetrievalStrategy] = []
        failed: list[str] = []
        first_exc: BaseException | None = None
        # union(dedup, first-seen) — RRF 가 하던 cross-strategy 융합 대신 reranker
        # 가 순위를 정하므로, 후보를 합치되 *순서는 reranker 에 위임*한다.
        pool: list[RetrievedChunk] = []
        seen: set[str] = set()
        had_list = False
        for strategy, res in zip(strategies, results, strict=True):
            if isinstance(res, BaseException):
                failed.append(strategy)
                if first_exc is None:
                    first_exc = res
                continue
            tool_results.append(res)
            executed.append(RetrievalStrategy(name=strategy, args_hash=res.input_hash or None))
            had_list = True
            out = RetrieverSearchOutput.model_validate(res.output or {})
            for c in out.chunks:
                # raw min_score 필터는 rerank *전*, 전략별 raw 리스트에 (계약 참조).
                if c.score < min_score or c.chunk_id in seen:
                    continue
                seen.add(c.chunk_id)
                pool.append(c)

        if not had_list:
            # 전 전략 실패 — 첫 예외를 그대로 올린다(required tool 의미 보존).
            assert first_exc is not None
            raise first_exc

        ranked_chunks, rerank_scores, rerank_result = await self._rerank(
            pool, query_text=query_text, top_k=top_k or fetch_k, ctx=ctx
        )
        if rerank_result is not None:
            tool_results.append(rerank_result)

        return DispatchResult(
            ranked_chunks=ranked_chunks,
            tool_results=tool_results,
            executed=executed,
            failed_strategies=failed,
            rerank_scores=rerank_scores,
        )

    async def _rerank(
        self,
        pool: list[RetrievedChunk],
        *,
        query_text: str,
        top_k: int,
        ctx: ToolExecutionContext,
    ) -> tuple[list[RetrievedChunk], dict[str, float], ToolResult | None]:
        """reranker(retriever.rerank) 로 후보 풀을 재정렬. 미배선/실패 시 union 순서로
        graceful degrade(rerank_scores 빈 dict). 반환: (순위 chunk, 점수, tool_result)."""
        if not pool:
            return [], {}, None
        try:
            res = await self._tools.invoke(
                "retriever.rerank",
                {
                    "query_text": query_text,
                    "candidates": [c.model_dump(mode="json") for c in pool],
                    "top_k": top_k,
                },
                ctx,
            )
        except Exception:  # noqa: BLE001 — 미배선(ToolUnknown) 등 → degrade.
            return (pool[:top_k] if top_k else pool), {}, None
        if res.status != "success" or not res.output:
            # required:false 라 실패해도 ToolResult 가 돌아온다 → record 는 하되 degrade.
            return (pool[:top_k] if top_k else pool), {}, res
        out = RerankOutput.model_validate(res.output)
        return list(out.chunks), dict(out.scores), res
