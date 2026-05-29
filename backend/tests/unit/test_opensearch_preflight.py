from __future__ import annotations

import httpx
import pytest

from app.adapters.tools.opensearch_preflight import OpenSearchPreflight


def _patch_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(
        "app.adapters.tools.opensearch_preflight.httpx.AsyncClient", factory
    )


_MAPPING_WITH_FIELDS = {
    "nrc-all-v1": {
        "mappings": {
            "properties": {
                "chunk_id": {"type": "keyword"},
                "clause_id": {"type": "keyword"},
                "authority_tier": {"type": "keyword"},
                "jurisdiction": {"type": "keyword"},
                "effective_on": {"type": "date"},
            }
        }
    }
}

_MAPPING_MISSING_FIELDS = {
    "nrc-all-v1": {"mappings": {"properties": {"chunk_id": {"type": "keyword"}}}}
}


def _handler_factory(mapping_body):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/_cluster/health"):
            return httpx.Response(200, json={"status": "green"})
        if path.endswith("/_mapping"):
            return httpx.Response(200, json=mapping_body)
        # HEAD /<index>
        return httpx.Response(200)

    return handler


async def test_required_fields_present_passes(monkeypatch):
    _patch_client(monkeypatch, _handler_factory(_MAPPING_WITH_FIELDS))
    check = OpenSearchPreflight(
        endpoint="http://os:9200",
        index="nrc-all-v1",
        severity="strict",
        required_fields=("clause_id", "authority_tier", "jurisdiction", "effective_on"),
    )
    result = await check.run()
    assert result.ok


async def test_required_fields_missing_fails_with_field_list(monkeypatch):
    _patch_client(monkeypatch, _handler_factory(_MAPPING_MISSING_FIELDS))
    check = OpenSearchPreflight(
        endpoint="http://os:9200",
        index="nrc-all-v1",
        severity="strict",
        required_fields=("clause_id", "authority_tier", "jurisdiction", "effective_on"),
    )
    result = await check.run()
    assert not result.ok
    assert set(result.details["missing_fields"]) == {
        "clause_id", "authority_tier", "jurisdiction", "effective_on",
    }


async def test_no_required_fields_skips_mapping_fetch(monkeypatch):
    """Default (v2) path: required_fields empty → no _mapping call, passes on
    health + index existence alone. Regression guard so v2 boot is unchanged."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/_cluster/health"):
            return httpx.Response(200, json={"status": "green"})
        return httpx.Response(200)

    _patch_client(monkeypatch, handler)
    check = OpenSearchPreflight(
        endpoint="http://os:9200", index="nrc-all-v1", severity="warn",
    )
    result = await check.run()
    assert result.ok
    assert not any(p.endswith("/_mapping") for p in calls)
