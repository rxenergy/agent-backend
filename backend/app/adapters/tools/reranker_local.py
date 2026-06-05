from __future__ import annotations

import re
from typing import Any

from app.domain.retrieval import RerankInput, RerankOutput
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext

# v3.1 Node 5 — `retriever.rerank` 로컬 fake(Phase 0–3). 실 cross-encoder
# (예: bge-reranker / Cohere rerank)는 후속 Phase 에서 OpenSearch 경로에 배선.
#
# fake 채점기는 *결정론* lexical overlap(질의 토큰 ∩ chunk 본문 토큰 / |질의|)을
# relevance 대용으로 쓴다. RRF 처럼 학습-free·재현 가능하며, 실 reranker 가 붙으면
# 같은 ToolPort 계약(RerankInput→RerankOutput)으로 무중단 교체된다(CLAUDE.md §1).

_WORD = re.compile(r"[0-9a-zA-Z가-힣]+")


def _tokens(text: str | None) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


class LocalRerankerTool:
    name = "retriever.rerank"
    version = "v1"

    async def invoke(
        self,
        tool_input: RerankInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RerankInput.model_validate(tool_input)

        q = _tokens(tool_input.query_text)

        def _relevance(chunk) -> float:
            if not q:
                return 0.0
            body = _tokens(chunk.text or chunk.snippet or "")
            return len(q & body) / len(q)

        # 결정론 정렬: relevance desc, 동점은 chunk_id asc(RRF 시절 tie-break 동형).
        ranked = sorted(
            tool_input.candidates,
            key=lambda c: (-_relevance(c), c.chunk_id),
        )
        if tool_input.top_k and tool_input.top_k > 0:
            ranked = ranked[: tool_input.top_k]
        scores = {c.chunk_id: round(_relevance(c), 6) for c in ranked}

        output = RerankOutput(chunks=ranked, scores=scores)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",  # filled by executor
            trace_id=context.trace_id,
        )
