from __future__ import annotations

import asyncio
from typing import Any

from app.domain.retrieval import RerankInput, RerankOutput
from app.domain.tools import ToolResult
from app.ports.embedding import SparseEncoderPort
from app.ports.tool import ToolExecutionContext

# v3.1 Node 5 — SPLADE 계열 sparse 모델 기반 reranker.
#
# 질의와 각 후보 본문을 같은 sparse encoder(FermiEncoder 등 SparseEncoderPort)로
# token→weight 희소 벡터로 인코딩하고, 공유 토큰 내적(Σ q_t·d_t)을 relevance 점수로
# 쓴다(SPLADE 채점). cross-encoder 와 달리 query×doc 쌍을 함께 넣지 않고 양측을 독립
# 인코딩하므로 후보 풀을 1회 배치 forward 로 채점할 수 있다.
#
# LocalRerankerTool(lexical overlap fake)의 *모델 기반* 대체 — 같은 retriever.rerank
# 도구 계약(RerankInput→RerankOutput)이라 dispatcher 는 무변경(CLAUDE.md §1). raw
# 내적 점수를 그대로 싣고(정규화는 Node 6 evaluator 가 max 로 수행) 순서가 권위다.


class SparseRerankerTool:
    name = "retriever.rerank"
    version = "v1"

    def __init__(self, sparse_encoder: SparseEncoderPort) -> None:
        self._encoder = sparse_encoder

    async def invoke(
        self,
        tool_input: RerankInput | dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if isinstance(tool_input, dict):
            tool_input = RerankInput.model_validate(tool_input)

        candidates = list(tool_input.candidates)
        if not candidates:
            return self._result(RerankOutput(chunks=[], scores={}), context)

        texts = [(c.text or c.snippet or "") for c in candidates]
        # 질의 + 후보 본문을 한 배치 forward 로 인코딩(torch CPU-bound → to_thread 로
        # 이벤트 루프 비차단). vectors[0]=질의, 이후가 후보 순서대로.
        vectors = await asyncio.to_thread(
            self._encoder.encode_documents, [tool_input.query_text, *texts]
        )
        q_vec = vectors[0]
        doc_vecs = vectors[1:]

        scored = [
            (self._dot(q_vec, d), c) for c, d in zip(candidates, doc_vecs)
        ]
        # 결정론 정렬: 점수 desc, 동점은 chunk_id asc (LocalRerankerTool 와 동형).
        scored.sort(key=lambda t: (-t[0], t[1].chunk_id))
        if tool_input.top_k and tool_input.top_k > 0:
            scored = scored[: tool_input.top_k]
        ranked = [c for _, c in scored]
        scores = {c.chunk_id: round(s, 6) for s, c in scored}

        return self._result(RerankOutput(chunks=ranked, scores=scores), context)

    @staticmethod
    def _dot(q: dict[str, float], d: dict[str, float]) -> float:
        """희소 벡터 내적 — 공유 토큰만 기여. 작은 dict 를 순회."""
        if not q or not d:
            return 0.0
        if len(d) < len(q):
            q, d = d, q
        return float(sum(w * d.get(t, 0.0) for t, w in q.items()))

    def _result(self, out: RerankOutput, context: ToolExecutionContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            tool_version=self.version,
            status="success",
            output=out.model_dump(mode="json"),
            latency_ms=0,
            input_hash="",  # filled by executor
            trace_id=context.trace_id,
        )
