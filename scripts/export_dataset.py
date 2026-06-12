#!/usr/bin/env python3
"""onprem Agent 실행 데이터를 분석용 단일 데이터셋으로 평탄화한다.

폐쇄망(air-gapped) 전제 — 외부 전송 없이 호스트 로컬 디스크로만 산출한다.
4개 인프라 요소에 분산된 한 turn 의 기록을 `interaction_id` 기준으로 조인:

  1. MinIO `interaction_events/*/events.jsonl`  ← fact 테이블 (v2 §16 스키마, 1 line = 1 turn)
  2. MinIO `context_snapshots/*/<iid>.json`      ← 검색 본문(snippet)·ContextPack
  3. MinIO `tool_result_records/*/<iid>.jsonl`   ← 도구(검색 포함) 호출 상태·해시·latency
  4. Postgres `tool_call_records`                ← 정규화 도구 레코드(중복 검증용, 선택)

산출: 한 turn 당 한 레코드의 JSONL (`dataset.jsonl`) + (pandas 있으면) Parquet.

입력은 `scripts/export_collect.sh` 가 호스트로 미리 내려받은 디렉토리 트리:
  <indir>/events/onprem/interaction_events/...
  <indir>/events/onprem/context_snapshots/...
  <indir>/events/onprem/tool_result_records/...

이 스크립트는 네트워크/도커에 접근하지 않는다(수집은 쉘이, 평탄화는 여기서).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _glob_events(root: Path) -> Iterator[dict[str, Any]]:
    """interaction_events/<day>/events.jsonl 전부를 turn 단위로 흘린다."""
    for p in sorted(root.rglob("interaction_events/*/events.jsonl")):
        yield from _iter_jsonl(p)


def _index_by_iid(root: Path, subdir: str) -> dict[str, Path]:
    """<subdir>/<day>/<interaction_id>.{json,jsonl} 를 iid → 최신 경로로."""
    out: dict[str, Path] = {}
    for p in sorted(root.rglob(f"{subdir}/*/*")):
        iid = p.stem  # <interaction_id>
        out[iid] = p  # sorted → 같은 iid 면 나중(=최신 day) 이 이긴다
    return out


def _load_context(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_tool_records(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.exists():
        return []
    return list(_iter_jsonl(path))


def flatten(events_root: Path) -> Iterator[dict[str, Any]]:
    ctx_idx = _index_by_iid(events_root, "context_snapshots")
    tool_idx = _index_by_iid(events_root, "tool_result_records")

    for ev in _glob_events(events_root):
        iid = ev.get("interaction_id")
        if not iid:
            continue
        ctx = _load_context(ctx_idx.get(iid))
        tools = _load_tool_records(tool_idx.get(iid))

        # 검색 본문은 InteractionEvent 에 없다(해시·id 만) — context_snapshot 에서 끌어온다.
        # context_snapshot 구조는 variant 마다 다르므로 통째로 싣고, 분석에서 자주
        # 쓰는 chunk 텍스트만 best-effort 로 평탄화한다.
        chunks = ctx.get("chunks") or ctx.get("context_chunks") or []
        retrieved_snippets = [
            {
                "chunk_id": c.get("chunk_id") or c.get("id"),
                "score": c.get("score"),
                "text": c.get("text") or c.get("snippet"),
                "document_id": c.get("document_id"),
            }
            for c in chunks
            if isinstance(c, dict)
        ]

        record = {
            # ── 조인 키 / 재현성 좌표 ──
            "interaction_id": iid,
            "trace_id": ev.get("trace_id"),
            "timestamp": ev.get("timestamp"),
            "app_profile": ev.get("app_profile"),
            "agent_variant": ev.get("agent_variant"),
            "model_id": ev.get("model_id"),
            # ── 질의 ──
            "query_text_sample": ev.get("query_text_sample"),
            "query_text_hash": ev.get("query_text_hash"),
            "scenario_object": ev.get("scenario_object"),
            "scenario_depth": ev.get("scenario_depth"),
            # ── 프롬프트 재현 ──
            "rendered_prompt_hash": ev.get("rendered_prompt_hash"),
            "prompt_version": ev.get("prompt_version"),
            "context_hash": ev.get("context_hash"),
            # ── 검색 기록 ──
            "retrieval_doc_count": ev.get("retrieval_doc_count"),
            "retrieved_chunk_ids": ev.get("retrieved_chunk_ids"),
            "retrieval_confidence": ev.get("retrieval_confidence"),
            "retrieved_snippets": retrieved_snippets,  # context_snapshot join 결과
            # ── 도구 호출(검색 포함) ──
            "tool_calls": ev.get("tool_calls"),
            "tool_result_records": tools,  # MinIO raw, latency/해시/error_code
            # ── 메모리 ──
            "memory_ids_used": ev.get("memory_ids_used"),
            "memory_types_used": ev.get("memory_types_used"),
            # ── 답변 / 검증 ──
            "answer_hash": ev.get("answer_hash"),
            "citation_ids": ev.get("citation_ids"),
            "verification_status": ev.get("verification_status"),
            "citation_completeness": ev.get("citation_completeness"),
            "faithfulness": ev.get("faithfulness"),
            "refusal_reason": ev.get("refusal_reason"),
            "error_code": ev.get("error_code"),
            # ── 비용 / 지연 ──
            "latency_ms": ev.get("latency_ms"),
            "token_usage": ev.get("token_usage"),
        }
        yield record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--indir",
        type=Path,
        required=True,
        help="export_collect.sh 가 내려받은 디렉토리 (events/ 트리를 포함)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="출력 JSONL 경로 (기본: <indir>/dataset.jsonl)",
    )
    ap.add_argument(
        "--parquet",
        action="store_true",
        help="pandas 가 설치돼 있으면 dataset.parquet 도 함께 쓴다",
    )
    args = ap.parse_args()

    events_root = args.indir / "events" if (args.indir / "events").exists() else args.indir
    out_path = args.out or (args.indir / "dataset.jsonl")

    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        rows: list[dict[str, Any]] = []
        for rec in flatten(events_root):
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            n += 1
            if args.parquet:
                rows.append(rec)

    print(f"[export] {n} turn(s) → {out_path}", file=sys.stderr)

    if args.parquet and n:
        try:
            import pandas as pd  # noqa: PLC0415

            pq = out_path.with_suffix(".parquet")
            # 중첩 컬럼(list/dict)은 JSON 문자열로 직렬화해 Parquet 호환 보장.
            df = pd.json_normalize(rows, max_level=0)
            for col in df.columns:
                if df[col].apply(lambda v: isinstance(v, (list, dict))).any():
                    df[col] = df[col].apply(
                        lambda v: json.dumps(v, ensure_ascii=False, default=str)
                        if isinstance(v, (list, dict))
                        else v
                    )
            df.to_parquet(pq, index=False)
            print(f"[export] {n} turn(s) → {pq}", file=sys.stderr)
        except ImportError:
            print("[export] pandas 미설치 — parquet 생략 (JSONL 만 생성됨)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
