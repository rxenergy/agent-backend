from __future__ import annotations

import pytest

from app.adapters.tools.verification_local import (
    LocalCitationCheckTool,
    LocalFaithfulnessCheckTool,
)
from app.ports.tool import ToolExecutionContext


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        interaction_id="i",
        trace_id="t",
        app_profile="local",
        agent_variant="agentic_finder_v4",
    )


@pytest.mark.asyncio
async def test_citation_check_full_match() -> None:
    tool = LocalCitationCheckTool()
    result = await tool.invoke(
        {
            "answer_text": "근거 [cite-0]와 [cite-1].",
            "citation_ids": ["cite-0", "cite-1"],
            "chunk_ids": ["c0", "c1"],
            "referenced_citation_ids": ["cite-0", "cite-1"],
            "resolvable_citation_ids": ["cite-0", "cite-1"],
        },
        _ctx(),
    )
    assert result.status == "success"
    assert result.output["citation_completeness"] == 1.0


@pytest.mark.asyncio
async def test_citation_check_half_match() -> None:
    tool = LocalCitationCheckTool()
    result = await tool.invoke(
        {
            "answer_text": "근거 [cite-0] 만.",
            "citation_ids": ["cite-0", "cite-1"],
            "chunk_ids": ["c0", "c1"],
            "referenced_citation_ids": ["cite-0", "cite-2"],
            "resolvable_citation_ids": ["cite-0", "cite-1"],
        },
        _ctx(),
    )
    # referenced={cite-0,cite-2}, usable={cite-0,cite-1} → matched={cite-0}
    assert result.output["citation_completeness"] == 0.5
    assert "cite-2" in result.output["unresolved_citation_ids"]


@pytest.mark.asyncio
async def test_citation_check_no_marker_in_answer() -> None:
    tool = LocalCitationCheckTool()
    result = await tool.invoke(
        {
            "answer_text": "근거 없이 짧게 답함",
            "citation_ids": ["cite-0"],
            "chunk_ids": ["c0"],
            "referenced_citation_ids": [],
        },
        _ctx(),
    )
    # 답변이 인용을 안 했으므로 completeness=0 — 단 tool 자체는 success.
    assert result.status == "success"
    assert result.output["citation_completeness"] == 0.0


@pytest.mark.asyncio
async def test_citation_check_unresolvable_citation() -> None:
    tool = LocalCitationCheckTool()
    result = await tool.invoke(
        {
            "answer_text": "근거 [cite-0]",
            "citation_ids": ["cite-0"],
            "chunk_ids": ["c0"],
            "referenced_citation_ids": ["cite-0"],
            "resolvable_citation_ids": [],
        },
        _ctx(),
    )
    # provided ∩ resolvable = ∅ → matched = ∅ → completeness=0.
    assert result.output["citation_completeness"] == 0.0


@pytest.mark.asyncio
async def test_faithfulness_check_baseline() -> None:
    tool = LocalFaithfulnessCheckTool()
    r = await tool.invoke(
        {"answer_text": "x", "chunk_ids": ["c0", "c1"]},
        _ctx(),
    )
    assert r.status == "success"
    assert 0.0 < r.output["faithfulness"] <= 1.0


@pytest.mark.asyncio
async def test_faithfulness_check_zero_chunks() -> None:
    tool = LocalFaithfulnessCheckTool()
    r = await tool.invoke({"answer_text": "x", "chunk_ids": []}, _ctx())
    assert r.output["faithfulness"] == 0.0
