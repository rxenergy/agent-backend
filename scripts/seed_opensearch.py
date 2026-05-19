#!/usr/bin/env python3
"""Index SMR seed JSONL into OpenSearch.

Usage (host):
    OPENSEARCH_ENDPOINT=http://localhost:9200 \
    OPENSEARCH_INDEX=smr-docs \
    SEED_FILE=datasets/seed_docs/smr_seed.jsonl \
    python3 scripts/seed_opensearch.py

Idempotent: deletes + recreates the index so re-runs always produce the same
document set. Uses _bulk for low-overhead indexing. No third-party dependency
(urllib only) so it can run on a bare Python 3.11 install.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "http://localhost:9200").rstrip("/")
INDEX = os.environ.get("OPENSEARCH_INDEX", "smr-docs")
SEED_FILE = Path(os.environ.get("SEED_FILE", "datasets/seed_docs/smr_seed.jsonl"))

INDEX_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "default": {"type": "standard"},
            }
        },
    },
    "mappings": {
        "properties": {
            "document_id": {"type": "keyword"},
            "chunk_id": {"type": "keyword"},
            "title": {"type": "text"},
            "page": {"type": "integer"},
            "section": {"type": "keyword"},
            "scenario_object": {"type": "keyword"},
            "text": {"type": "text"},
        }
    },
}


def request(method: str, path: str, body: bytes | None = None, content_type: str = "application/json") -> tuple[int, bytes]:
    req = urllib.request.Request(f"{ENDPOINT}{path}", data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def recreate_index() -> None:
    status, _ = request("DELETE", f"/{INDEX}")
    if status not in (200, 404):
        raise SystemExit(f"DELETE /{INDEX} → {status}")
    body = json.dumps(INDEX_MAPPING).encode("utf-8")
    status, payload = request("PUT", f"/{INDEX}", body)
    if status >= 300:
        raise SystemExit(f"PUT /{INDEX} → {status}: {payload!r}")
    print(f"  recreated index '{INDEX}'")


def bulk_index(docs: list[dict]) -> None:
    lines: list[bytes] = []
    for doc in docs:
        meta = {"index": {"_index": INDEX, "_id": doc["chunk_id"]}}
        lines.append(json.dumps(meta).encode("utf-8"))
        lines.append(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    body = b"\n".join(lines) + b"\n"
    status, payload = request("POST", "/_bulk?refresh=true", body, content_type="application/x-ndjson")
    if status >= 300:
        raise SystemExit(f"_bulk → {status}: {payload[:512]!r}")
    parsed = json.loads(payload)
    if parsed.get("errors"):
        raise SystemExit(f"_bulk had errors: {payload[:512]!r}")
    print(f"  indexed {len(docs)} chunks")


def main() -> int:
    if not SEED_FILE.exists():
        print(f"seed file not found: {SEED_FILE}", file=sys.stderr)
        return 2
    docs = [json.loads(line) for line in SEED_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"OpenSearch: {ENDPOINT}  index: {INDEX}  seed: {SEED_FILE} ({len(docs)} docs)")
    recreate_index()
    bulk_index(docs)
    # Verify count
    status, payload = request("GET", f"/{INDEX}/_count")
    if status >= 300:
        raise SystemExit(f"_count → {status}: {payload!r}")
    print(f"  _count: {payload.decode('utf-8').strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
