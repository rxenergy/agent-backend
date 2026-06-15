#!/usr/bin/env python3
"""spec_driven_v1 의 N3 Retrieval 단계를 CLI 에서 단독 실험.

runner(SpecDrivenRunner.run, N3)가 부르는 것과 *동일한* 도구를 조립해 한 번 호출한다:
  retrieval.search = RetrievalSearchTool(retriever=OpenSearchRetrieverTool,
                                         reranker=IdentityReranker, fetch_k=retrieval_fetch_k)
즉 하이브리드 검색(E5 dense + Fermi sparse + BM25) → identity rerank → top_k 절단.
ToolExecutor 는 거치지 않는다(정책/타임아웃/스팬은 실험 범위 밖) — 검색 자체만 본다.

입력 페이로드는 N3 가 retrieval.search 에 싣는 dict 와 동형이다:
  {query_text, top_k, target, min_token_count, filters{noise, collection, ...}}

연결 정보는 Settings(=env)에서 온다. 컨테이너 밖 호스트에서 돌리려면
OPENSEARCH_ENDPOINT 를 localhost:9200 으로 덮어쓴다(컨테이너 기본은 opensearch:9200).

사용::

    # stdin 으로 페이로드 JSON 주입(권장)
    OPENSEARCH_ENDPOINT=http://localhost:9200 \
    OPENSEARCH_INDEX=nrc-all-v3 \
    python3 scripts/exp_retrieval.py < payload.json

    # 또는 인라인
    echo '{"query_text":"...","top_k":10,"filters":{"noise":false}}' \
      | OPENSEARCH_ENDPOINT=http://localhost:9200 python3 scripts/exp_retrieval.py

torch/sentence-transformers/transformers([embeddings] extra)가 깔린 venv 가 필요하다
(첫 실행 시 E5/Fermi 모델 다운로드). 호스트에 없으면 agent-api 컨테이너 안에서 돌린다.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# host 레이아웃(scripts/ 옆 backend/) / 컨테이너 레이아웃(/app 에 app 패키지) 모두 지원.
_HERE = Path(__file__).resolve().parent.parent
for _cand in (_HERE / "backend", _HERE):
    if (_cand / "app" / "config" / "settings.py").exists():
        sys.path.insert(0, str(_cand))
        break


async def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: 페이로드 JSON 을 stdin 으로 주입하세요.", file=sys.stderr)
        return 2
    payload = json.loads(raw)

    from app.adapters.embeddings.e5 import E5Encoder
    from app.adapters.embeddings.fermi import FermiEncoder
    from app.adapters.reranker.identity import IdentityReranker
    from app.adapters.tools.retrieval_search import RetrievalSearchTool
    from app.adapters.tools.retriever_opensearch import OpenSearchRetrieverTool
    from app.config.settings import Settings
    from app.ports.tool import ToolExecutionContext

    s = Settings()  # env(.env 미로딩 — env 변수만)에서 연결 정보 로드.
    print(
        f"[exp] endpoint={s.opensearch_endpoint} index={s.opensearch_index} "
        f"pipeline={s.opensearch_search_pipeline} k_dense={s.retriever_k_dense} "
        f"fetch_k={s.retrieval_fetch_k} device={s.embedding_device}",
        file=sys.stderr,
    )

    # runner 가 OpenSearch 경로에서 주입하는 것과 동일한 인코더/도구 조립.
    dense = E5Encoder(
        model_id=s.embedding_e5_model,
        device=s.embedding_device,
        max_seq_len=s.embedding_e5_max_seq_len,
    )
    sparse = FermiEncoder(
        model_id=s.embedding_fermi_model,
        device=s.embedding_device,
        max_seq_len=s.embedding_fermi_max_seq_len,
        top_n=s.embedding_fermi_top_n,
    )
    dense.warmup()
    sparse.warmup()

    retriever = OpenSearchRetrieverTool(
        endpoint=s.opensearch_endpoint,
        index=s.opensearch_index,
        dense_encoder=dense,
        sparse_encoder=sparse,
        search_pipeline=s.opensearch_search_pipeline or None,
        dense_field=s.opensearch_dense_field,
        sparse_field=s.opensearch_sparse_field,
        text_field=s.opensearch_text_field,
        k_dense=s.retriever_k_dense,
        username=s.opensearch_username or None,
        password=s.opensearch_password or None,
        verify_certs=s.opensearch_verify_certs,
        snippet_chars=s.opensearch_snippet_chars,
    )
    search = RetrievalSearchTool(
        retriever=retriever, reranker=IdentityReranker(), fetch_k=s.retrieval_fetch_k
    )

    ctx = ToolExecutionContext(
        interaction_id="exp-retrieval", trace_id="",
        app_profile=s.app_profile, agent_variant="spec_driven_v1",
        session_id=None, user_id=None, project_id=None,
    )

    result = await search.invoke(payload, ctx)
    chunks = (result.output or {}).get("chunks", []) if result.output else []
    rerank_scores = (result.output or {}).get("rerank_scores", []) if result.output else []

    print(f"\n[exp] status={result.status} num_chunks={len(chunks)}", file=sys.stderr)
    if result.status == "failed":
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 1

    for i, c in enumerate(chunks):
        rr = rerank_scores[i] if i < len(rerank_scores) else None
        body = c.get("text") or c.get("snippet") or ""
        print(
            f"\n#{i + 1}  score={c.get('score'):.4f}"
            + (f" rerank={rr:.4f}" if isinstance(rr, (int, float)) else "")
            + f"\n  chunk_id : {c.get('chunk_id')}"
            f"\n  doc      : {c.get('document_id')}  (p={c.get('page')}, §{c.get('section')})"
            f"\n  title    : {c.get('title')}"
            f"\n  body[:240]: {body[:240].replace(chr(10), ' ')}"
        )

    # 전체 결과(JSON)를 stdout 으로 — 파이프/jq 분석용.
    print("\n" + json.dumps(result.output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
