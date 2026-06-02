from __future__ import annotations

import hashlib
from typing import Any

from app.domain.retrieval import (
    RetrievedChunk,
    RetrieverSearchInput,
    RetrieverSearchOutput,
)
from app.domain.tools import ToolResult
from app.ports.tool import ToolExecutionContext


class LocalRetrieverTool:
    name = "retriever.search"
    version = "v1"

    async def invoke(
        self,
        tool_input: RetrieverSearchInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RetrieverSearchInput.model_validate(tool_input)

        seed = hashlib.sha256(tool_input.query_text.encode("utf-8")).hexdigest()
        # 현실 반영: 검색으로 올라온 chunk 는 질의어·엔티티를 포함한다(BM25/dense 가
        # 그래서 올렸다). snippet 에 query_text + entity 값을 넣어 Node 6 의 lexical/
        # entity 신호가 fake 경로에서도 의미를 갖게 한다(없으면 evaluator 가 전량
        # WEAK/FAIL → 로컬 dev/데모가 깨짐).
        entity_terms = " ".join(
            v for vs in (tool_input.entities or {}).values() for v in vs if v
        )
        chunks = []
        for i in range(max(1, tool_input.top_k)):
            snippet = f"[fake {i}] {tool_input.query_text} {entity_terms}".strip()
            chunks.append(
                RetrievedChunk(
                    chunk_id=f"chunk-{seed[:8]}-{i}",
                    document_id=f"doc-{seed[:6]}",
                    score=round(0.9 - i * 0.1, 3),
                    page=10 + i,
                    section=f"§{i + 1}",
                    snippet=snippet,
                    # 노이즈 floor 가 fake 경로에서도 작동하도록 token_count 부여
                    # (본문 토큰 수 근사 = snippet 단어수).
                    token_count=len(snippet.split()),
                )
            )
        # v3.1 노이즈 floor(Layer 2) — OpenSearch 없이도 floor 를 검증할 수 있게
        # post-filter. min_token_count==0 이면 무효(기존 동작 보존).
        if tool_input.min_token_count and tool_input.min_token_count > 0:
            chunks = [
                c for c in chunks
                if (c.token_count or 0) >= tool_input.min_token_count
            ]
        output = RetrieverSearchOutput(chunks=chunks)
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=output.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",  # filled by executor
            trace_id=context.trace_id,
        )
