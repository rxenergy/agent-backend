from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.application.retrieval.rrf import reciprocal_rank_fusion, rrf_scores
from app.domain.retrieval import (
    RetrievalPlan,
    RetrievalStrategy,
    RetrievedChunk,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext

# v3.1 Node 5 — retrieval_execute. 코드가 전략별로 retriever.search 를 병렬
# 호출(asyncio.gather)하고 RRF 로 융합한다. LLM 미사용.
#
# 융합 후 계약(downstream 가 반드시 지켜야 함):
#   • `fused_chunks` 의 *순서*가 권위(authoritative) — RRF 순위다.
#   • `RetrievedChunk.score` 는 raw retriever 점수이며 융합 순서와 일치하지
#     않는다(non-monotonic). 다운스트림은 *위치* 또는 `rrf_scores` 로 순위를
#     매기고, `chunk.score` 로 순위를 매기지 않는다.
#   • raw `min_score` 필터는 *융합 전, 전략별 raw 리스트*에 적용한다 — 융합
#     결과를 raw 로 거르면 cross-strategy agreement 가 높은(=RRF 상위) chunk 를
#     raw 가 낮다는 이유로 떨어뜨려 방금 한 융합과 충돌한다.


@dataclass
class DispatchResult:
    fused_chunks: list[RetrievedChunk]
    tool_results: list[ToolResult] = field(default_factory=list)  # 성공한 전략 호출(record 용)
    executed: list[RetrievalStrategy] = field(default_factory=list)  # 성공 전략 메타
    failed_strategies: list[str] = field(default_factory=list)
    rrf_scores: dict[str, float] = field(default_factory=dict)  # chunk_id → 융합 점수(권위 순위)


class RetrievalDispatcher:
    """RetrievalPlan 의 전략들을 fan-out 실행하고 RRF 융합.

    부분 실패 내성: 일부 전략이 실패(RequiredToolFailed 등 예외)해도 ≥1 전략이
    성공하면 성공분만 융합한다(다전략 redundancy 의 목적). 전부 실패하면 첫 예외를
    re-raise → conductor 가 RETRIEVAL_NO_RESULT refusal 로 매핑."""

    def __init__(self, tool_executor, *, rrf_k: int = 60) -> None:
        self._tools = tool_executor
        self._rrf_k = rrf_k

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
        """전략별로 `fetch_k` 깊이로 검색 → raw min_score 전략별 필터 → RRF 융합.

        `fetch_k` 는 *전략별 후보 풀 깊이*(spec Node 5 ~20). 최종 다운스트림 개수와
        분리한다 — 깊게 가져와 융합해야 RRF 가 더 나은 상위를 고른다. `top_k` 가
        주어지면 융합 결과를 그 길이로 자르지만(상한), 보통 conductor 가 자른다."""
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

        ranked_lists: list[list[RetrievedChunk]] = []
        tool_results: list[ToolResult] = []
        executed: list[RetrievalStrategy] = []
        failed: list[str] = []
        first_exc: BaseException | None = None
        for strategy, res in zip(strategies, results, strict=True):
            if isinstance(res, BaseException):
                failed.append(strategy)
                if first_exc is None:
                    first_exc = res
                continue
            tool_results.append(res)
            executed.append(RetrievalStrategy(name=strategy, args_hash=res.input_hash or None))
            out = RetrieverSearchOutput.model_validate(res.output or {})
            # raw min_score 필터는 융합 *전*, 전략별 raw 리스트에 (계약 참조).
            ranked_lists.append([c for c in out.chunks if c.score >= min_score])

        if not ranked_lists:
            # 전 전략 실패 — 첫 예외를 그대로 올린다(required tool 의미 보존).
            assert first_exc is not None
            raise first_exc

        fused = reciprocal_rank_fusion(ranked_lists, k=self._rrf_k, top_k=top_k)
        return DispatchResult(
            fused_chunks=fused,
            tool_results=tool_results,
            executed=executed,
            failed_strategies=failed,
            rrf_scores=rrf_scores(ranked_lists, k=self._rrf_k),
        )
