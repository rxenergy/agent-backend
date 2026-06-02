from __future__ import annotations

from app.adapters.tools.retriever_local import LocalRetrieverTool
from app.ports.tool import ToolExecutionContext


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="i", trace_id="t", app_profile="local",
        agent_variant="hierarchical_corrective_v3_1",
    )


async def test_local_sets_token_count_on_chunks():
    out = await LocalRetrieverTool().invoke(
        {"query_text": "reactor safety system", "top_k": 2}, _ctx()
    )
    chunks = out.output["chunks"]
    assert chunks and all(c["token_count"] and c["token_count"] > 0 for c in chunks)


async def test_local_noise_floor_drops_short_chunks():
    # 생성 snippet 단어수(~5)보다 높은 floor → 전 chunk 제외(floor 작동 증명).
    out = await LocalRetrieverTool().invoke(
        {"query_text": "q", "top_k": 3, "min_token_count": 50}, _ctx()
    )
    assert out.output["chunks"] == []


async def test_local_floor_zero_keeps_chunks():
    out = await LocalRetrieverTool().invoke(
        {"query_text": "reactor safety", "top_k": 3, "min_token_count": 0}, _ctx()
    )
    assert len(out.output["chunks"]) == 3
