"""Integration test harness for OpenSearch-backed tool adapters.

These tests talk to a real OpenSearch instance. They are opt-in: set
`OPENSEARCH_TEST_ENDPOINT` (e.g. `http://localhost:9200` when `make up-local`
is running) to enable them. Otherwise the entire module is skipped.

Each test session creates a dedicated, isolated index (`smr-docs-itest`) so
the dev `smr-docs` index seeded by `make seed` is not disturbed.
"""

from __future__ import annotations

import os
from typing import Iterator

import httpx
import pytest

ITEST_INDEX = "smr-docs-itest"

_INDEX_BODY = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "document_id": {"type": "keyword"},
            "chunk_id": {"type": "keyword"},
            "title": {"type": "text"},
            "page": {"type": "integer"},
            "section": {"type": "keyword"},
            "scenario_object": {"type": "keyword"},
            "doc_type": {"type": "keyword"},
            "revision": {"type": "keyword"},
            "response_date": {"type": "keyword"},
            "text": {"type": "text"},
        }
    },
}

_SEED_DOCS = [
    {
        "document_id": "rg-1-157",
        "chunk_id": "rg-1-157#sec4.2#1",
        "title": "RG 1.157 — Best-Estimate ECCS",
        "page": 12,
        "section": "Section 4.2",
        "scenario_object": "O2",
        "doc_type": "regulation",
        "revision": "Rev. 3 (2017)",
        "text": "Peak cladding temperature shall not exceed 2200 F under any postulated LOCA. Best-estimate calculation with 95/95 uncertainty.",
    },
    {
        "document_id": "nuscale-dc-tier2",
        "chunk_id": "nuscale-dc-tier2#ch6.2.3#1",
        "title": "NuScale DC Tier 2 — Passive Containment Cooling",
        "page": 645,
        "section": "6.2.3",
        "scenario_object": "O1",
        "doc_type": "vendor",
        "revision": "Rev. 5",
        "text": "The NuScale Passive Containment Cooling System removes decay heat via natural convection of the reactor pool water. No AC power required for 72 hours.",
    },
    {
        "document_id": "nuscale-rai-1234",
        "chunk_id": "nuscale-rai-1234#response#1",
        "title": "NuScale RAI #1234 — DWO Response",
        "page": 8,
        "section": "Response 1",
        "scenario_object": "O3",
        "doc_type": "rai",
        "revision": "Rev. 0",
        "response_date": "2018-05-15",
        "text": "Density wave oscillation stability analyses using TRACE were performed. Decay ratio remained below 0.5 across the full operating envelope.",
    },
]


def _endpoint() -> str | None:
    return os.environ.get("OPENSEARCH_TEST_ENDPOINT")


@pytest.fixture(scope="session")
def opensearch_endpoint() -> str:
    ep = _endpoint()
    if not ep:
        pytest.skip("OPENSEARCH_TEST_ENDPOINT not set; integration tests skipped")
    return ep.rstrip("/")


@pytest.fixture(scope="session")
def opensearch_index(opensearch_endpoint: str) -> Iterator[str]:
    """Create and seed an isolated index; drop on teardown."""
    base = opensearch_endpoint
    with httpx.Client(timeout=10.0) as client:
        client.delete(f"{base}/{ITEST_INDEX}")  # 404 ok
        r = client.put(f"{base}/{ITEST_INDEX}", json=_INDEX_BODY)
        r.raise_for_status()
        lines: list[str] = []
        for doc in _SEED_DOCS:
            lines.append(f'{{"index":{{"_index":"{ITEST_INDEX}","_id":"{doc["chunk_id"]}"}}}}')
            import json as _json

            lines.append(_json.dumps(doc, ensure_ascii=False))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        r = client.post(
            f"{base}/_bulk?refresh=true",
            content=body,
            headers={"Content-Type": "application/x-ndjson"},
        )
        r.raise_for_status()
        assert not r.json().get("errors"), r.text[:512]

    yield ITEST_INDEX

    with httpx.Client(timeout=10.0) as client:
        client.delete(f"{base}/{ITEST_INDEX}")


@pytest.fixture
def tool_context():
    from app.ports.tool import ToolExecutionContext

    return ToolExecutionContext(
        interaction_id="itest-inter",
        trace_id="itest-trace",
        app_profile="local",
        agent_variant="agentic_finder_v4",
    )
