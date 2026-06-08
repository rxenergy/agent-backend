# OpenSearch nrc-all-v1 원격 배포 RUNBOOK

본 기기(Atomchat-SaaS)에서 생성된 OpenSearch fs snapshot 을 원격 배포 서버
`rx@192.169.200.13` 의 `~/agent-backend` 환경에 복원해서 사용한다.

데이터:
- **인덱스**: `nrc-all-v1` (NRC ADAMS/govinfo + NuScale, 690,589 docs, dense_e5 + sparse_fermi)
- **snapshot 패키지**: `~/opensearch_snapshots_nrc-all-v1.tar.gz` (~4.2GB) + `.sha256`
- **출하용 snapshot 이름**: `nrc-all-v1-ship-20260528-1331` (최신, 동봉된 패키지 안에 포함)

> 본 RUNBOOK 의 모든 명령은 원격 서버 셸에서 실행하는 것을 전제로 한다.

---

## 0. 사전 준비 — 본 기기에서

이미 완료되었으나 재생성 필요 시:

```bash
# Atomchat-SaaS 측에서
cd /home/rx/Project/Atomchat-SaaS
curl -s -XPOST "http://localhost:9200/nrc-all-v1/_flush?wait_if_ongoing=true"
curl -s -XPUT "http://localhost:9200/_snapshot/nrc-snapshots/nrc-all-v1-ship-$(date +%Y%m%d-%H%M)?wait_for_completion=true" \
  -H 'Content-Type: application/json' \
  -d '{"indices":"nrc-all-v1","include_global_state":false}'

cd indexing/docker/opensearch/single_gpu
tar -czf ~/opensearch_snapshots_nrc-all-v1.tar.gz snapshots/
sha256sum ~/opensearch_snapshots_nrc-all-v1.tar.gz > ~/opensearch_snapshots_nrc-all-v1.tar.gz.sha256
```

---

## 1. 패키지 전송 (사용자 환경에 맞게 수단 선택)

### 옵션 A — scp

```bash
scp ~/opensearch_snapshots_nrc-all-v1.tar.gz* rx@192.169.200.13:~/
```

### 옵션 B — rsync (재시도 안정성)

```bash
rsync -avP --partial \
  ~/opensearch_snapshots_nrc-all-v1.tar.gz \
  ~/opensearch_snapshots_nrc-all-v1.tar.gz.sha256 \
  rx@192.169.200.13:~/
```

### 옵션 C — 외장 디스크 / 클라우드 (사내 망 차단 시)

USB 또는 사내 사설 클라우드를 통해 옮긴 뒤 원격 서버의 `~` 에 둔다.

---

## 2. 원격 서버 — 무결성 검증

```bash
ssh rx@192.169.200.13
cd ~
sha256sum -c opensearch_snapshots_nrc-all-v1.tar.gz.sha256
# → 출력: opensearch_snapshots_nrc-all-v1.tar.gz: OK
```

OK가 나오지 않으면 다시 전송 (rsync `--partial` 권장).

---

## 3. 원격 서버 — agent-backend 최신화

```bash
cd ~/agent-backend
git pull --ff-only
# 본 작업의 매핑/init.sh/pipelines/retriever 변경이 포함된 커밋이 들어왔는지 확인
git log -1 --stat | head -20
```

---

## 4. 원격 서버 — snapshot 디렉토리 준비 및 압축 해제

```bash
sudo mkdir -p /srv/os-snapshots
sudo chown 1000:1000 /srv/os-snapshots
sudo tar -xzf ~/opensearch_snapshots_nrc-all-v1.tar.gz --strip-components=1 -C /srv/os-snapshots
sudo chown -R 1000:1000 /srv/os-snapshots

# 검증: 본 snapshot fs 의 핵심 파일이 있는지
ls -la /srv/os-snapshots
# → indices/, snap-*.dat, meta-*.dat, index-N, index.latest 가 보여야 한다
```

권한 (`1000:1000`) 은 OpenSearch 컨테이너 내부의 opensearch 사용자 uid 와 맞추기 위함.

---

## 5. 원격 서버 — 외부 볼륨 생성

compose.onprem.yml 에서 `opensearch_data` 가 `external: true` 로 선언되어 있으므로
컨테이너 기동 전에 미리 만들어야 한다.

```bash
docker volume create opensearch-data-single-gpu
docker volume inspect opensearch-data-single-gpu  # Mountpoint 확인
```

---

## 6. 원격 서버 — OpenSearch 컨테이너 기동 (onprem 프로파일)

OpenSearch 만 우선 띄운다 (다른 서비스는 별도 단계에서 추가).

```bash
cd ~/agent-backend
docker compose \
  --env-file infra/env/onprem.env \
  -f infra/compose/compose.yml \
  -f infra/compose/compose.onprem.yml \
  --profile onprem \
  up -d opensearch

# 기동 로그 — "started" 메시지 확인
docker compose -f infra/compose/compose.yml -f infra/compose/compose.onprem.yml logs -f opensearch | \
  grep -E "started|cluster_name|path.repo" | head -10
```

기동 후 healthcheck 통과까지 약 20–30 초 소요.

---

## 7. 원격 서버 — snapshot repository 등록 + restore

```bash
ENDPOINT=http://localhost:9200

# repository 등록 (idempotent)
curl -XPUT "$ENDPOINT/_snapshot/nrc-snapshots" \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "fs",
    "settings": {
      "location": "/usr/share/opensearch/snapshots",
      "compress": true
    }
  }'

# 사용 가능한 snapshot 목록 확인 → ship 패키지의 latest 이름 확인
curl -s "$ENDPOINT/_snapshot/nrc-snapshots/_all?pretty" | grep '"snapshot"'

# 가장 최신 ship 스냅샷으로 restore (이름은 위 출력에서 확인 후 대입)
SNAP=nrc-all-v1-ship-20260528-1331   # ← 실제 이름으로 교체
curl -XPOST "$ENDPOINT/_snapshot/nrc-snapshots/${SNAP}/_restore?wait_for_completion=true" \
  -H 'Content-Type: application/json' \
  -d '{"indices":"nrc-all-v1","include_aliases":false}'
```

restore가 success로 끝나면 즉시 인덱스 사용 가능.

---

## 8. 원격 서버 — search pipelines 4개 등록 + 인덱스 매핑 보정

snapshot 으로 인덱스는 이미 채워졌으므로 init.sh 의 `PUT index` 단계는
200 응답을 받고 skip 한다. 단 search pipelines 4개는 PUT 으로 등록해야 한다.

```bash
cd ~/agent-backend
OPENSEARCH_ENDPOINT=http://localhost:9200 \
OPENSEARCH_INDEX=nrc-all-v1 \
  sh infra/opensearch/init.sh
```

### 등록된 pipeline 확인

```bash
curl -s "$ENDPOINT/_search/pipeline" | python3 -c "
import json, sys
for name in json.load(sys.stdin):
    print(' ', name)
"
# 출력 예상 (weights 순서 = [BM25, dense, sparse]):
#   nrc-hybrid-search        (0.2, 0.3, 0.5)   # 폴백 기본
#   nrc-hybrid-search-k5      (0.4, 0.3, 0.3)   # operating point top_k=5 벤치마크
#   nrc-hybrid-search-k10     (0.4, 0.2, 0.4)   # operating point top_k=10 벤치마크
#   nrc-hybrid-bm25-only     (1.0, 0.0, 0.0)
#   nrc-hybrid-dense-only    (0.0, 1.0, 0.0)
#   nrc-hybrid-sparse-only   (0.0, 0.0, 1.0)
#
# hybrid pipeline 은 retriever_top_k(operating point)에 연동되어 선택된다
# (profiles.py): k=5→k5, k=10→k10, 그 외→nrc-hybrid-search.
```

---

## 9. 검증

### 9.1 doc 카운트 + search_type 분포

```bash
ENDPOINT=http://localhost:9200

curl -s "$ENDPOINT/_cat/indices/nrc-all-v1?v"
curl -s "$ENDPOINT/nrc-all-v1/_count" | python3 -m json.tool
# expected: count ≈ 690589

for ST in manual nuscale; do
  N=$(curl -s -H "Content-Type: application/json" "$ENDPOINT/nrc-all-v1/_count" \
    -d "{\"query\":{\"term\":{\"search_type\":\"$ST\"}}}" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["count"])')
  echo "  search_type=$ST: $N"
done
# expected: manual=412632, nuscale=277957
```

### 9.2 매핑 핵심 필드 확인

```bash
curl -s "$ENDPOINT/nrc-all-v1/_mapping?filter_path=**.properties.search_type,**.properties.doc_metadata.dynamic" \
  | python3 -m json.tool
# expected: search_type.type = "keyword", doc_metadata.dynamic = "true"
```

### 9.3 샘플 hybrid 쿼리 (BM25 + dense + sparse 결합)

> agent-backend 안에서 호출하는 retriever 가 정상 동작하는지 sanity.
> 단순 BM25 쿼리는 dense/sparse 벡터 인코딩 없이도 가능:

```bash
curl -s -H "Content-Type: application/json" \
  "$ENDPOINT/nrc-all-v1/_search?search_pipeline=nrc-hybrid-bm25-only" \
  -d '{
    "size": 3,
    "_source": ["chunk_id","collection","search_type","section_path_str"],
    "query": {
      "hybrid": {
        "queries": [
          {"match": {"text": "reactor coolant pressure boundary"}}
        ]
      }
    }
  }' | python3 -m json.tool
```

### 9.4 backend retriever 통합 sanity

agent-api 컨테이너를 띄운 후 (또는 호스트의 venv 에서) RetrieverTool 호출:

```bash
cd ~/agent-backend
uv run --directory backend python - <<'PY'
import asyncio
from app.adapters.tools.retriever_opensearch import OpenSearchRetrieverTool
from app.ports.tool import ToolExecutionContext

class _Dense:
    dim = 1024
    def encode_query(self, t): return [0.0] * 1024  # 실 e5 인코딩 대체 (sanity 용)
    def warmup(self): pass
class _Sparse:
    def encode_query(self, t): return {"reactor": 1.0, "coolant": 0.8}
    def warmup(self): pass

async def main():
    tool = OpenSearchRetrieverTool(
        endpoint="http://localhost:9200",
        index="nrc-all-v1",
        dense_encoder=_Dense(),
        sparse_encoder=_Sparse(),
        search_pipeline="nrc-hybrid-search",
    )
    ctx = ToolExecutionContext(
        interaction_id="smoke-1", trace_id="t-1",
        app_profile="onprem", agent_variant="agentic_finder_v4",
    )
    r = await tool.invoke(
        {"query_text": "reactor coolant pressure boundary", "top_k": 3,
         "scenario_object": "regulation"},
        ctx,
    )
    print("status:", r.status)
    for c in r.output["chunks"]:
        print(f"  {c['chunk_id']} score={c['score']:.4f} col={c['collection']} st={c['search_type']}")
        print(f"    section: {c['section']}")

asyncio.run(main())
PY
```

`status: success` + 3개 chunk 표시되면 OK.

---

## 10. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `repository_exception: cannot create blob store` | `/srv/os-snapshots` 권한 불일치. `sudo chown -R 1000:1000 /srv/os-snapshots` |
| `snapshot_missing_exception` | 압축 해제 후 `/srv/os-snapshots` 안에 `index-N`/`indices/` 가 보이는지. `--strip-components=1` 빠지면 한 단계 더 깊어진다 |
| `volume "opensearch-data-single-gpu" not found` | 단계 5 의 `docker volume create` 누락 |
| `bootstrap.memory_lock` 관련 경고 | 호스트 ulimit memlock 미설정. 운영에 큰 영향 없으나 `/etc/security/limits.conf` 에 `* hard memlock unlimited` 추가 권장 |
| restore 시 인덱스 이미 존재 | `curl -XDELETE $ENDPOINT/nrc-all-v1` 한 뒤 다시 restore |
| retriever 가 빈 결과 반환 | `search_pipeline` 인자 누락. compose env 의 `OPENSEARCH_SEARCH_PIPELINE` 확인 (디폴트 `nrc-hybrid-search`) |
| dense_e5 dim mismatch | 본 인덱스는 1024-dim (e5-large). 다른 인코더로 query 보내면 차원 오류 |

---

## 11. 정리 (작업 후)

```bash
# 패키지는 더 이상 필요 없을 때만
rm ~/opensearch_snapshots_nrc-all-v1.tar.gz
rm ~/opensearch_snapshots_nrc-all-v1.tar.gz.sha256

# snapshot fs 는 향후 재복원/추가 백업 위해 /srv/os-snapshots 그대로 유지
```

이상으로 본 기기에서 만든 nrc-all-v1 색인 데이터를 원격 서버의
agent-backend onprem 스택에서 그대로 사용 가능한 상태가 된다.
