#!/usr/bin/env sh
# OpenSearch fs-snapshot 관리 — local 프로파일 전용.
#
# 스냅샷 본체는 프로젝트의 infra/opensearch/snapshots/ (컨테이너 내부
# /usr/share/opensearch/snapshots) 에 저장된다. 이 경로는 opensearch.yml 의
# path.repo 화이트리스트와 compose.local.yml 의 바인드 마운트로 연결된다.
# 본체는 .gitignore 로 저장소 커밋에서 제외된다 (수 GB).
#
# 환경변수:
#   OPENSEARCH_ENDPOINT   default http://localhost:9200
#   OS_SNAPSHOT_REPO      default local_fs
#   OS_SNAPSHOT_INDICES   default nrc-*  (예: "nrc-all-v3" | "nrc-*" | "*")
#                         — 배포된 인덱스(v1/v3 등)를 버전 무관하게 포착하고
#                           create/restore 를 대칭으로 유지하기 위한 glob.
#
# 사용법:
#   sh scripts/opensearch_snapshot.sh create  <snapshot-name>
#   sh scripts/opensearch_snapshot.sh restore <snapshot-name>
#   sh scripts/opensearch_snapshot.sh list
set -eu

ENDPOINT="${OPENSEARCH_ENDPOINT:-http://localhost:9200}"
REPO="${OS_SNAPSHOT_REPO:-local_fs}"
INDICES="${OS_SNAPSHOT_INDICES:-nrc-*}"
LOCATION="/usr/share/opensearch/snapshots"

register_repo() {
  curl -fsS -X PUT "${ENDPOINT}/_snapshot/${REPO}" \
    -H 'Content-Type: application/json' \
    -d "{\"type\":\"fs\",\"settings\":{\"location\":\"${LOCATION}\",\"compress\":true}}" >/dev/null
  echo "[snapshot] repository '${REPO}' registered (location=${LOCATION})"
}

cmd_create() {
  name="${1:?usage: create <snapshot-name>}"
  register_repo
  curl -fsS -X PUT "${ENDPOINT}/_snapshot/${REPO}/${name}?wait_for_completion=true" \
    -H 'Content-Type: application/json' \
    -d "{\"indices\":\"${INDICES}\",\"include_global_state\":false}"
  echo
  echo "[snapshot] created '${name}' (indices=${INDICES})"
}

cmd_restore() {
  name="${1:?usage: restore <snapshot-name>}"
  register_repo
  # 복원 전 대상 인덱스를 닫는다 (열린 인덱스는 덮어쓸 수 없음). 없으면 무시.
  curl -fsS -X POST "${ENDPOINT}/${INDICES}/_close" >/dev/null 2>&1 || true
  curl -fsS -X POST "${ENDPOINT}/_snapshot/${REPO}/${name}/_restore?wait_for_completion=true" \
    -H 'Content-Type: application/json' \
    -d "{\"indices\":\"${INDICES}\",\"include_global_state\":false}"
  echo
  echo "[snapshot] restored '${name}' (indices=${INDICES})"
}

cmd_list() {
  register_repo
  curl -fsS "${ENDPOINT}/_snapshot/${REPO}/_all?pretty"
}

case "${1:-}" in
  create)  shift; cmd_create  "$@" ;;
  restore) shift; cmd_restore "$@" ;;
  list)    shift; cmd_list    "$@" ;;
  *) echo "usage: $0 {create|restore|list} [snapshot-name]" >&2; exit 2 ;;
esac
