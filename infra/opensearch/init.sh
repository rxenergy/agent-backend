#!/usr/bin/env sh
# Initialize the OpenSearch nrc-all-v3 index and nrc-hybrid-search pipeline.
#
# Idempotent: PUT on an existing index/pipeline replaces the prior definition
# (mapping changes that conflict with existing data will fail — drop the index
# first if you need a destructive reset).
#
# Required env:
#   OPENSEARCH_ENDPOINT    default http://localhost:9200
#   OPENSEARCH_INDEX       default nrc-all-v3
#   OPENSEARCH_SEARCH_PIPELINE  default nrc-hybrid-search
#
# Usage:
#   sh infra/opensearch/init.sh
set -eu

ENDPOINT="${OPENSEARCH_ENDPOINT:-http://localhost:9200}"
INDEX="${OPENSEARCH_INDEX:-nrc-all-v3}"
PIPELINE="${OPENSEARCH_SEARCH_PIPELINE:-nrc-hybrid-search}"

# Resolve mapping/pipeline JSON paths relative to this script so the same
# command works from host (`make opensearch-init`) and from inside containers.
HERE="$(cd "$(dirname "$0")" && pwd)"
MAPPING="${HERE}/mappings/nrc-all-v3.json"
PIPELINE_JSON="${HERE}/pipelines/nrc-hybrid-search.json"

echo "[opensearch-init] endpoint=${ENDPOINT} index=${INDEX} pipeline=${PIPELINE}"

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

# 2) Search pipeline — PUT is idempotent.
echo "  upserting search pipeline '${PIPELINE}'"
curl -fsS -X PUT "${ENDPOINT}/_search/pipeline/${PIPELINE}" \
  -H 'Content-Type: application/json' \
  --data-binary "@${PIPELINE_JSON}" >/dev/null

echo "[opensearch-init] done"
