"""Live OpenSearch integration tests for the retriever tool.

Skipped unless OPENSEARCH_TEST_ENDPOINT is set. See conftest.py.
"""

from __future__ import annotations

import pytest

from app.adapters.tools.retriever_opensearch import OpenSearchRetrieverTool

pytestmark = pytest.mark.integration


async def test_retriever_bm25_basic_query(opensearch_endpoint, opensearch_index, tool_context):
    tool = OpenSearchRetrieverTool(endpoint=opensearch_endpoint, index=opensearch_index)
    result = await tool.invoke(
        {"query_text": "peak cladding temperature LOCA", "top_k": 3}, tool_context
    )
    assert result.status == "success"
    chunks = result.output["chunks"]
    assert chunks, "expected at least one hit for ECCS/PCT query"
    assert chunks[0]["chunk_id"] == "rg-1-157#sec4.2#1"
    assert chunks[0]["doc_type"] == "regulation"
    assert chunks[0]["revision"] == "Rev. 3 (2017)"


async def test_retriever_scenario_object_filter(opensearch_endpoint, opensearch_index, tool_context):
    """O1 filter must exclude O2/O3 docs even when query terms match them."""
    tool = OpenSearchRetrieverTool(endpoint=opensearch_endpoint, index=opensearch_index)
    result = await tool.invoke(
        {
            "query_text": "decay heat removal",
            "top_k": 5,
            "scenario_object": "O1",
        },
        tool_context,
    )
    chunks = result.output["chunks"]
    assert chunks, "expected at least one O1 hit"
    for c in chunks:
        # Filter must enforce O1 only; doc_type for O1 fixtures is 'vendor'.
        assert c["doc_type"] == "vendor"


async def test_retriever_entity_constrains_to_match(
    opensearch_endpoint, opensearch_index, tool_context
):
    """Entity terms join the must clause: only docs containing the entity survive."""
    tool = OpenSearchRetrieverTool(endpoint=opensearch_endpoint, index=opensearch_index)
    result = await tool.invoke(
        {
            "query_text": "stability analysis",
            "top_k": 5,
            # "TRACE" appears only in the RAI doc body in the seed corpus.
            "entities": {"keywords": ["TRACE"]},
        },
        tool_context,
    )
    chunks = result.output["chunks"]
    assert chunks
    assert chunks[0]["chunk_id"] == "nuscale-rai-1234#response#1"
    assert chunks[0]["response_date"] == "2018-05-15"


async def test_retriever_response_date_passthrough(
    opensearch_endpoint, opensearch_index, tool_context
):
    tool = OpenSearchRetrieverTool(endpoint=opensearch_endpoint, index=opensearch_index)
    result = await tool.invoke({"query_text": "TRACE decay ratio", "top_k": 1}, tool_context)
    chunks = result.output["chunks"]
    assert chunks
    assert chunks[0]["response_date"] == "2018-05-15"
    assert chunks[0]["revision"] == "Rev. 0"
