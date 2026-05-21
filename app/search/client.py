from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from opensearchpy import OpenSearch

logger = logging.getLogger(__name__)


def build_client(url: str) -> OpenSearch:
    parsed = urlparse(url)
    use_ssl = parsed.scheme == "https"
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if use_ssl else 9200)
    client = OpenSearch(
        hosts=[{"host": host, "port": port}],
        http_compress=True,
        use_ssl=use_ssl,
        verify_certs=False,
        ssl_show_warn=False,
        timeout=30,
    )
    logger.info("OpenSearch client built: %s:%d (ssl=%s)", host, port, use_ssl)
    return client


def cluster_status(client: OpenSearch) -> str:
    try:
        health = client.cluster.health(timeout=2)
        return health.get("status", "unknown")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenSearch health check failed: %s", exc)
        return "unreachable"


def execute_search(
    client: OpenSearch,
    index: str,
    dsl: dict[str, Any],
    pipeline: str | None = None,
) -> dict[str, Any]:
    params = {"search_pipeline": pipeline} if pipeline else None
    return client.search(index=index, body=dsl, params=params)
