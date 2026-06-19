#!/usr/bin/env sh
# Phoenix SQLite 콜드 백업 / 복원 — onprem(및 local 테스트) 전용.
#
# 배경:
#   Phoenix 는 PHOENIX_SQL_DATABASE_URL 미설정 시 PHOENIX_WORKING_DIR
#   (= /data/phoenix, compose 의 phoenix_data 볼륨) 아래 SQLite(phoenix.db)
#   를 쓴다. Phoenix 는 SQLite -> Postgres 데이터 이전 도구를 제공하지 않고,
#   7.x -> 17.x 업그레이드는 v9 annotation 마이그레이션 등 in-place 손상
#   리스크가 있다. 따라서 Postgres 백엔드로 전환하기 전에 기존 trace 를
#   "콜드 아카이브" 로 남겨, 필요 시 옛 버전(7.12) 인스턴스로 읽기전용 조회한다.
#
# 일관성:
#   phoenix 이미지는 distroless 라 컨테이너 내부에 sqlite3/sh 가 없다.
#   호스트 sqlite3 도 가정하지 않는다. 그래서 WAL(-wal/-shm)이 본 파일에
#   머지되도록 *컨테이너를 멈춘 뒤* /data/phoenix 를 통째로 추출한다. 이렇게
#   하면 sqlite3 없이도 일관된 스냅샷이 된다 (재기동 시 WAL 이 정상 replay).
#
#   추출은 `docker cp` 로 한다 — 호스트 볼륨 경로(/var/lib/docker/...)는
#   Docker Desktop/WSL2 등에서 호스트 셸에 노출되지 않으므로(Docker VM 내부),
#   볼륨 드라이버/플랫폼 무관하게 동작하는 docker cp 가 견고하다.
#
# 환경변수:
#   PHOENIX_CONTAINER   default phoenix          (정지/기동 대상 컨테이너 이름)
#   PHOENIX_BACKUP_DIR  default infra/backups/phoenix
#
# 사용법:
#   sh scripts/phoenix_backup.sh backup [label]   # 정지->복사(tar.gz)->기동
#   sh scripts/phoenix_backup.sh list
#   sh scripts/phoenix_backup.sh extract <archive> <dest-dir>   # 옛 데이터 풀기
set -eu

CONTAINER="${PHOENIX_CONTAINER:-phoenix}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
BACKUP_DIR="${PHOENIX_BACKUP_DIR:-${REPO_ROOT}/infra/backups/phoenix}"

cmd_backup() {
  label="${1:-manual}"

  if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "[phoenix-backup] ERROR: 컨테이너 '${CONTAINER}' 를 찾지 못했습니다." >&2
    exit 1
  fi

  mkdir -p "${BACKUP_DIR}"
  # 타임스탬프는 호스트 date 로 스탬프 (재현성 영향 없음, 운영 편의).
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  archive="${BACKUP_DIR}/phoenix-${label}-${stamp}.tar.gz"

  was_running=0
  if [ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo false)" = "true" ]; then
    was_running=1
  fi

  # WAL 머지 일관성을 위해 컨테이너를 멈춘다.
  if [ "${was_running}" = "1" ]; then
    echo "[phoenix-backup] stopping '${CONTAINER}' for a consistent snapshot..."
    docker stop "${CONTAINER}" >/dev/null
  fi

  # docker cp 로 /data/phoenix 전체(phoenix.db + WAL/-shm + exports/inferences/
  # trace_datasets)를 호스트 임시 디렉토리로 추출 후 tar.gz 로 묶는다.
  tmp=$(mktemp -d)
  trap 'rm -rf "${tmp}"' EXIT
  echo "[phoenix-backup] extracting ${CONTAINER}:/data/phoenix ..."
  docker cp "${CONTAINER}:/data/phoenix/." "${tmp}/"

  # 재기동은 추출 직후 즉시 (압축 중 다운타임 최소화).
  if [ "${was_running}" = "1" ]; then
    echo "[phoenix-backup] restarting '${CONTAINER}'..."
    docker start "${CONTAINER}" >/dev/null
  fi

  if [ ! -f "${tmp}/phoenix.db" ]; then
    echo "[phoenix-backup] WARN: phoenix.db 가 추출물에 없습니다 (이미 Postgres 백엔드?)." >&2
  fi

  echo "[phoenix-backup] archiving -> ${archive}"
  tar -C "${tmp}" -czf "${archive}" .

  size=$(du -h "${archive}" | cut -f1)
  echo "[phoenix-backup] DONE: ${archive} (${size})"
  echo "[phoenix-backup] 이 아카이브는 옛 Phoenix(7.12) 인스턴스에 마운트하면 읽기전용 조회 가능 (docs 참조)."
}

cmd_list() {
  mkdir -p "${BACKUP_DIR}"
  ls -lh "${BACKUP_DIR}"/*.tar.gz 2>/dev/null || echo "[phoenix-backup] (no backups in ${BACKUP_DIR})"
}

cmd_extract() {
  archive="${1:?usage: extract <archive> <dest-dir>}"
  dest="${2:?usage: extract <archive> <dest-dir>}"
  mkdir -p "${dest}"
  tar -C "${dest}" -xzf "${archive}"
  echo "[phoenix-backup] extracted ${archive} -> ${dest}"
  echo "[phoenix-backup] 옛 데이터 조회: docs/references/phoenix_performance_tuning.md 의 '콜드 아카이브 복원' 절 참조."
}

case "${1:-}" in
  backup)  shift; cmd_backup  "$@" ;;
  list)    shift; cmd_list    "$@" ;;
  extract) shift; cmd_extract "$@" ;;
  *) echo "usage: $0 {backup|list|extract} [...]" >&2; exit 2 ;;
esac
