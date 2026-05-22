#!/usr/bin/env python3
"""Index seed JSONL into OpenSearch ``nrc-all-v3`` (hybrid mapping).

Two modes:

* default — index text + metadata only (BM25 works; dense/sparse subqueries
  fall back to ``match_none``-style behavior). Useful for fast iteration
  without loading torch.
* ``--encode`` — load E5 + Fermi encoders, compute ``dense_e5`` /
  ``sparse_fermi`` per chunk, and index everything for full hybrid scoring.
  Requires the backend's ``[embeddings]`` extra (``torch``,
  ``sentence-transformers``, ``transformers``) to be installed.

The index/pipeline themselves are created by ``infra/opensearch/init.sh``;
this script assumes the mapping already exists and only bulk-indexes docs.
Set ``--recreate`` to drop and rebuild the index from the mapping file.

Usage::

    OPENSEARCH_ENDPOINT=http://localhost:9200 \
    OPENSEARCH_INDEX=nrc-all-v3 \
    SEED_FILE=datasets/seed_docs/smr_seed.jsonl \
    python3 scripts/seed_opensearch.py [--encode] [--recreate]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "http://localhost:9200").rstrip("/")
INDEX = os.environ.get("OPENSEARCH_INDEX", "nrc-all-v3")
SEED_FILE = Path(os.environ.get("SEED_FILE", "datasets/seed_docs/smr_seed.jsonl"))
MAPPING_FILE = Path(
    os.environ.get(
        "OPENSEARCH_MAPPING_FILE",
        "infra/opensearch/mappings/nrc-all-v3.json",
    )
)


def request(
    method: str, path: str, body: bytes | None = None, content_type: str = "application/json"
) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{ENDPOINT}{path}", data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def recreate_index() -> None:
    if not MAPPING_FILE.exists():
        raise SystemExit(f"mapping file not found: {MAPPING_FILE}")
    status, _ = request("DELETE", f"/{INDEX}")
    if status not in (200, 404):
        raise SystemExit(f"DELETE /{INDEX} → {status}")
    body = MAPPING_FILE.read_bytes()
    status, payload = request("PUT", f"/{INDEX}", body)
    if status >= 300:
        raise SystemExit(f"PUT /{INDEX} → {status}: {payload!r}")
    print(f"  recreated index '{INDEX}' from {MAPPING_FILE}")


def _load_encoders():
    """Lazy import the backend embedding adapters (torch is heavy).

    Supports two layouts:
      * host repo — scripts/ alongside backend/, so backend/ holds the `app` pkg
      * container — Dockerfile copies `app` package to /app/app, with scripts/
        mounted at /app/scripts; the parent of scripts/ already holds `app/`
    """
    here = Path(__file__).resolve().parent.parent
    for candidate in (here / "backend", here):
        if (candidate / "app" / "adapters" / "embeddings" / "e5.py").exists():
            sys.path.insert(0, str(candidate))
            break
    from app.adapters.embeddings.e5 import E5Encoder  # noqa: WPS433
    from app.adapters.embeddings.fermi import FermiEncoder  # noqa: WPS433

    e5 = E5Encoder(
        model_id=os.environ.get("EMBEDDING_E5_MODEL", "intfloat/multilingual-e5-large"),
        device=os.environ.get("EMBEDDING_DEVICE", "cpu"),
        max_seq_len=int(os.environ.get("EMBEDDING_E5_MAX_SEQ_LEN", "512")),
    )
    fermi = FermiEncoder(
        model_id=os.environ.get("EMBEDDING_FERMI_MODEL", "atomic-canyon/fermi-1024"),
        device=os.environ.get("EMBEDDING_DEVICE", "cpu"),
        max_seq_len=int(os.environ.get("EMBEDDING_FERMI_MAX_SEQ_LEN", "1024")),
        top_n=int(os.environ.get("EMBEDDING_FERMI_TOP_N", "200")),
    )
    print(f"  encoders loaded: e5 dim={e5.dim}")
    return e5, fermi


def _enrich_with_embeddings(docs: list[dict], e5, fermi) -> None:
    """Compute dense_e5 + sparse_fermi for each doc in place."""
    for i, doc in enumerate(docs):
        text = doc.get("text", "") or ""
        # Encoders treat passages and queries identically here — the seed
        # corpus is small enough that we accept the small e5 mismatch
        # (`passage: ` prefix) by calling the proper API.
        doc["dense_e5"] = e5.encode_passages([text])[0]
        doc["sparse_fermi"] = fermi.encode_query(text)
        if (i + 1) % 25 == 0:
            print(f"  encoded {i + 1}/{len(docs)}")


def bulk_index(docs: list[dict]) -> None:
    lines: list[bytes] = []
    for doc in docs:
        meta = {"index": {"_index": INDEX, "_id": doc["chunk_id"]}}
        lines.append(json.dumps(meta).encode("utf-8"))
        lines.append(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    body = b"\n".join(lines) + b"\n"
    status, payload = request(
        "POST", "/_bulk?refresh=true", body, content_type="application/x-ndjson"
    )
    if status >= 300:
        raise SystemExit(f"_bulk → {status}: {payload[:512]!r}")
    parsed = json.loads(payload)
    if parsed.get("errors"):
        raise SystemExit(f"_bulk had errors: {payload[:512]!r}")
    print(f"  indexed {len(docs)} chunks")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--encode",
        action="store_true",
        help="compute dense_e5 + sparse_fermi via backend embedding adapters",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="DELETE + PUT the index using the mapping file before bulk indexing",
    )
    args = parser.parse_args()

    if not SEED_FILE.exists():
        print(f"seed file not found: {SEED_FILE}", file=sys.stderr)
        return 2
    docs = [
        json.loads(line)
        for line in SEED_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(
        f"OpenSearch: {ENDPOINT}  index: {INDEX}  seed: {SEED_FILE} "
        f"({len(docs)} docs, encode={args.encode})"
    )

    if args.recreate:
        recreate_index()

    if args.encode:
        e5, fermi = _load_encoders()
        _enrich_with_embeddings(docs, e5, fermi)

    bulk_index(docs)

    status, payload = request("GET", f"/{INDEX}/_count")
    if status >= 300:
        raise SystemExit(f"_count → {status}: {payload!r}")
    print(f"  _count: {payload.decode('utf-8').strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
