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

    def __init__(self, *, retriever: Tool, reranker: RerankerPort) -> None:
        self._retriever = retriever
        self._reranker = reranker

    async def invoke(
        self,
        tool_input: RetrieverSearchInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)

        inner = await self._retriever.invoke(tool_input, context)
        if inner.status == "failed":
            # 내부 검색 실패는 그대로 전파(executor 가 retrieval.search 의 required
            # 정책으로 처리). reranker 는 타지 않는다.
            return inner.model_copy(update={"tool_name": self.name, "tool_version": self.version})

        raw_chunks = (inner.output or {}).get("chunks", []) or []
        chunks = [RetrievedChunk.model_validate(c) for c in raw_chunks]
        ranked = await self._reranker.rerank(
            tool_input.query_text, chunks, top_k=tool_input.top_k
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
