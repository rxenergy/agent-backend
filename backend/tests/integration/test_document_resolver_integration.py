"""Live OpenSearch integration tests for document.resolve_citation."""

from __future__ import annotations

import pytest

from app.adapters.tools.document_opensearch import OpenSearchDocumentResolverTool

pytestmark = pytest.mark.integration


async def test_resolver_marks_found_and_missing(
    opensearch_endpoint, opensearch_index, tool_context
):
    tool = OpenSearchDocumentResolverTool(endpoint=opensearch_endpoint, index=opensearch_index)
    result = await tool.invoke(
        {
            "citation_ids": ["c1", "c2", "c3"],
            "chunk_ids": [
                "rg-1-157#sec4.2#1",
                "nuscale-rai-1234#response#1",
                "definitely-missing#x#1",
            ],
        },
        tool_context,
    )
    resolved = result.output["resolved"]
    by_chunk = {r["chunk_id"]: r for r in resolved}

    found = by_chunk["rg-1-157#sec4.2#1"]
    assert found["resolvable"] is True
    assert found["document_id"] == "rg-1-157"
    assert found["page"] == 12

    rai = by_chunk["nuscale-rai-1234#response#1"]
    assert rai["resolvable"] is True
    assert rai["document_id"] == "nuscale-rai-1234"

    missing = by_chunk["definitely-missing#x#1"]
    assert missing["resolvable"] is False
    assert missing["document_id"] is None


async def test_resolver_all_missing_returns_unresolvable(
    opensearch_endpoint, opensearch_index, tool_context
):
    tool = OpenSearchDocumentResolverTool(endpoint=opensearch_endpoint, index=opensearch_index)
    result = await tool.invoke(
        {
            "citation_ids": ["c1", "c2"],
            "chunk_ids": ["nope-1", "nope-2"],
        },
        tool_context,
    )
    resolved = result.output["resolved"]
    assert len(resolved) == 2
    assert all(r["resolvable"] is False for r in resolved)
