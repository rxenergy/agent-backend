#!/usr/bin/env bash
# onprem Agent 실행 데이터를 호스트 로컬 디스크로 한 번에 수집한다.
#
# 폐쇄망(air-gapped) 전제 — 외부로 전송하지 않고, 떠 있는 onprem 컨테이너의
# 내부 포트/볼륨에서 읽어 ./export/<stamp>/ 아래로만 떨군다.
#
# 수집 대상 (CLAUDE.md "재현 가능성" 좌표를 가로지름):
#   1. MinIO  smr-agent-events 버킷 전체  → events/ (interaction/context/tool/prompt)
#   2. Postgres agent_state               → agent_state.dump (tool_call_records·memory)
#   3. Phoenix phoenix.db (트레이스/실험)  → phoenix/
#   4. (선택) Tempo 볼륨                    → tempo.tgz
#
# 그 후 scripts/export_dataset.py 로 interaction_id 기준 분석용 단일 데이터셋 생성.
#
# Usage:
#   scripts/export_collect.sh                 # 전체, ./export/<UTC stamp>/ 로
#   OUTDIR=/srv/dump scripts/export_collect.sh # 출력 위치 지정
#   NEWER_THAN=7d scripts/export_collect.sh    # 최근 7일 MinIO 객체만 미러
#   SKIP_TEMPO=1 scripts/export_collect.sh     # Tempo 볼륨 tar 생략
set -euo pipefail

# ── 설정 (onprem.env 기본값과 일치) ──────────────────────────────────────────
MINIO_CONTAINER="${MINIO_CONTAINER:-minio}"
PG_CONTAINER="${PG_CONTAINER:-postgres}"
PHOENIX_CONTAINER="${PHOENIX_CONTAINER:-phoenix}"
EVENT_BUCKET="${EVENT_BUCKET:-smr-agent-events}"
MINIO_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:-minioadmin}"
PG_USER="${PG_USER:-agent}"
PG_DB="${PG_DB:-agent_state}"
NEWER_THAN="${NEWER_THAN:-}"     # 예: 7d, 24h. 비면 전체.
SKIP_TEMPO="${SKIP_TEMPO:-0}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTDIR="${OUTDIR:-./export/${STAMP}}"
mkdir -p "${OUTDIR}/events"

echo "[collect] 출력 디렉토리: ${OUTDIR}"

# ── 1. MinIO — 이벤트 버킷 전체 미러 ─────────────────────────────────────────
# minio/mc 컨테이너 안에서 mc mirror 를 돌리고, 결과를 호스트로 docker cp.
# (mc 가 호스트에 설치돼 있으면 그쪽을 직접 써도 됨.)
echo "[collect] 1/4 MinIO ${EVENT_BUCKET} → events/"
MIRROR_ARGS=""
[ -n "${NEWER_THAN}" ] && MIRROR_ARGS="--newer-than ${NEWER_THAN}"
docker run --rm --network container:"${MINIO_CONTAINER}" \
  -v "$(cd "${OUTDIR}/events" && pwd)":/out \
  --entrypoint /bin/sh \
  minio/mc:latest -c "
    mc alias set src http://localhost:9000 '${MINIO_USER}' '${MINIO_PASS}' >/dev/null &&
    mc mirror ${MIRROR_ARGS} --overwrite src/${EVENT_BUCKET} /out
  "

# ── 2. Postgres — tool_call_records / session_memory / candidate / approved ──
echo "[collect] 2/4 Postgres ${PG_DB} → agent_state.dump"
docker exec "${PG_CONTAINER}" pg_dump -U "${PG_USER}" -d "${PG_DB}" -Fc \
  -f /tmp/agent_state.dump
docker cp "${PG_CONTAINER}:/tmp/agent_state.dump" "${OUTDIR}/agent_state.dump"
docker exec "${PG_CONTAINER}" rm -f /tmp/agent_state.dump
# 분석에서 바로 쓰도록 핵심 테이블은 CSV 로도 한 벌 뽑는다.
for t in tool_call_records session_memory approved_memories; do
  docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" \
    -c "\copy (SELECT * FROM ${t}) TO STDOUT WITH CSV HEADER" \
    > "${OUTDIR}/${t}.csv" 2>/dev/null || echo "  (skip ${t} — 미존재)"
done

# ── 3. Phoenix — 트레이스/실험 SQLite ────────────────────────────────────────
echo "[collect] 3/4 Phoenix phoenix.db → phoenix/"
docker cp "${PHOENIX_CONTAINER}:/data/phoenix" "${OUTDIR}/phoenix" \
  || echo "  (skip Phoenix — 컨테이너/경로 미존재)"

# ── 4. Tempo — span 볼륨 (선택) ──────────────────────────────────────────────
if [ "${SKIP_TEMPO}" != "1" ]; then
  echo "[collect] 4/4 Tempo 볼륨 → tempo.tgz"
  docker run --rm \
    -v "$(basename "$(pwd)")_tempo_data":/d:ro \
    -v "$(cd "${OUTDIR}" && pwd)":/o \
    alpine sh -c "tar czf /o/tempo.tgz -C /d . 2>/dev/null" \
    || echo "  (skip Tempo — 볼륨명 불일치 시 TEMPO_VOLUME 지정)"
else
  echo "[collect] 4/4 Tempo 건너뜀 (SKIP_TEMPO=1)"
fi

# ── 5. 분석용 단일 데이터셋 평탄화 ───────────────────────────────────────────
echo "[collect] 평탄화 → dataset.jsonl (+parquet if pandas)"
python3 "$(dirname "$0")/export_dataset.py" --indir "${OUTDIR}" --parquet \
  || echo "  (평탄화 실패 — raw 는 ${OUTDIR}/events 에 보존됨)"

echo "[collect] 완료: ${OUTDIR}"
ls -la "${OUTDIR}"
