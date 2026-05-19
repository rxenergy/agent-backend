#!/bin/sh
set -e

ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"
USER="${MINIO_ROOT_USER:-minioadmin}"
PASS="${MINIO_ROOT_PASSWORD:-minioadmin}"
BUCKET="${EVENT_BUCKET:-smr-agent-events}"

# Wait for MinIO
i=0
until mc alias set local "$ENDPOINT" "$USER" "$PASS" >/dev/null 2>&1; do
  i=$((i+1))
  if [ "$i" -gt 30 ]; then
    echo "MinIO not reachable at $ENDPOINT" >&2
    exit 1
  fi
  sleep 2
done

mc mb --ignore-existing "local/$BUCKET"
echo "Bucket ready: $BUCKET"
