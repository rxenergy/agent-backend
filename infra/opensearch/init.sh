#!/usr/bin/env sh
# Initialize the OpenSearch nrc-all-v1 index and hybrid search pipelines.
#
# Idempotent: PUT on an existing index/pipeline replaces the prior definition
# (mapping changes that conflict with existing data will fail — drop the index
# first if you need a destructive reset). Snapshot restore가 이미 인덱스를
# 채운 경우 PUT index 단계는 200 응답을 받고 skip 된다.
#
# Required env:
#   OPENSEARCH_ENDPOINT    default http://localhost:9200
#   OPENSEARCH_INDEX       default nrc-all-v1
#
# Usage:
#   sh infra/opensearch/init.sh
set -eu

ENDPOINT="${OPENSEARCH_ENDPOINT:-http://localhost:9200}"
INDEX="${OPENSEARCH_INDEX:-nrc-all-v1}"

# Resolve mapping/pipeline JSON paths relative to this script so the same
# command works from host (`make opensearch-init`) and from inside containers.
HERE="$(cd "$(dirname "$0")" && pwd)"
MAPPING="${HERE}/mappings/nrc-all-v1.json"
PIPELINE_DIR="${HERE}/pipelines"

echo "[opensearch-init] endpoint=${ENDPOINT} index=${INDEX}"

# 1) Index — create if missing.
status="$(curl -s -o /dev/null -w '%{http_code}' "${ENDPOINT}/${INDEX}")"
if [ "${status}" = "200" ]; then
  echo "  index '${INDEX}' already exists (skip)"
else
  echo "  creating index '${INDEX}'"
  curl -fsS -X PUT "${ENDPOINT}/${INDEX}" \
    -H 'Content-Type: application/json' \
    --data-binary "@${MAPPING}" >/dev/null
fi

# 2) Search pipelines — pipelines/ 디렉토리의 모든 *.json 을 idempotent PUT.
#    파일명(stem)이 그대로 pipeline id 가 된다.
for f in "${PIPELINE_DIR}"/*.json; do
  [ -f "$f" ] || continue
  name="$(basename "$f" .json)"
  echo "  upserting search pipeline '${name}'"
  curl -fsS -X PUT "${ENDPOINT}/_search/pipeline/${name}" \
    -H 'Content-Type: application/json' \
    --data-binary "@${f}" >/dev/null
done

echo "[opensearch-init] done"
