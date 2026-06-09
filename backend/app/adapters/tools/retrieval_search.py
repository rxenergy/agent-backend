from __future__ import annotations

from typing import Any

from app.domain.retrieval import (
    RetrievedChunk,
    RetrieverSearchInput,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.reranker import RerankerPort
from app.ports.tool import Tool, ToolExecutionContext


class RetrievalSearchTool:
    """agentic_finder `retrieval.search` — 하이브리드 검색 → **Reranker 정렬**(설계
    finder §2/§3, RRF 대체). 별도 도구가 아니라 *어댑터 내부 정렬*이다: 내부 retriever
    Tool(검색 어댑터 재사용)을 직접 호출해 chunk 를 얻고, RerankerPort 로 query 관련성
    재정렬 후 같은 출력 스키마(RetrieverSearchOutput)로 돌려준다. reranker 점수는
    `rerank_scores` 로 chunks 와 같은 순서로 실어 FinderRound 계측 입력이 된다.

    내부 retriever Tool 은 ToolExecutor 를 *거치지 않고* 직접 호출한다 — retrieval.search
    자체가 ToolExecutor 가 라우팅하는 하나의 논리 도구이므로 이중 executor 래핑을
    피한다(policy/timeout/span 은 retrieval.search 호출에 한 번 적용)."""

    name = "retrieval.search"
    version = "v1"

    def __init__(
        self, *, retriever: Tool, reranker: RerankerPort, fetch_k: int = 20
    ) -> None:
        self._retriever = retriever
        self._reranker = reranker
        # retrieve-then-rerank 후보 풀 깊이 — 최종 top_k 와 *분리*. 내부 retriever 를
        # top_k 만큼만 fetch 하면 reranker 가 검색이 이미 고른 같은 집합을 받아 재정렬이
        # no-op 이 된다(690k 코퍼스에서 top_k=3 이면 3→3). fetch_k 깊이로 받아 reranker 가
        # 그 풀에서 상위 top_k 를 고르게 한다(Nogueira & Cho retrieve-then-rerank).
        # v3.1 dispatcher 와 동일한 retrieval_fetch_k 시맨틱. fetch_k<=top_k 면 passthrough.
        self._fetch_k = fetch_k

    async def invoke(
        self,
        tool_input: RetrieverSearchInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)

        # 후보 풀(fetch_k)로 깊게 fetch → reranker 가 상위 top_k 절단. final_k 는
        # 호출자가 요청한 최종 개수로 보존한다(rerank trim 키).
        final_k = tool_input.top_k
        pool_k = max(final_k, self._fetch_k)
        retr_input = (
            tool_input if pool_k == final_k
            else tool_input.model_copy(update={"top_k": pool_k})
        )
        inner = await self._retriever.invoke(retr_input, context)
        if inner.status == "failed":
            # 내부 검색 실패는 그대로 전파(executor 가 retrieval.search 의 required
            # 정책으로 처리). reranker 는 타지 않는다.
            return inner.model_copy(update={"tool_name": self.name, "tool_version": self.version})

        raw_chunks = (inner.output or {}).get("chunks", []) or []
        chunks = [RetrievedChunk.model_validate(c) for c in raw_chunks]
        ranked = await self._reranker.rerank(
            tool_input.query_text, chunks, top_k=final_k
        )
        output = RetrieverSearchOutput(
            chunks=[r.chunk for r in ranked],
            rerank_scores=[r.rerank_score for r in ranked],
        )
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",  # filled by executor
            trace_id=context.trace_id,
        )
