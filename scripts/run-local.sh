#!/bin/sh
# Bring up the local profile stack from the repo root.
set -e
cd "$(dirname "$0")/.."

docker compose \
  --env-file infra/env/local.env \
  --profile local \
  -f infra/compose/compose.yml \
  -f infra/compose/compose.local.yml \
  up -d --build

echo
echo "Stack up. URLs:"
echo "  agent-api      http://localhost:8000/health"
echo "  Phoenix        http://localhost:6006"
echo "  Grafana        http://localhost:3000"
echo "  MinIO console  http://localhost:9001  (minioadmin/minioadmin)"
