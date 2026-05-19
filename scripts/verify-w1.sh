#!/usr/bin/env bash
# Phase W1 manual verification checklist.
# Runs through the eight DoD items from docs/plans/agent_mvp_worklog.md §W1.
#
# Usage:
#   scripts/verify-w1.sh
#
# Env (override as needed):
#   BASE=http://localhost:8000
#   OPENSEARCH_ENDPOINT=http://localhost:9200
#   OPENSEARCH_INDEX=smr-docs
#   PG_URL=postgres://agent:agent@localhost:5432/agent_state
#   MINIO_ENDPOINT=http://localhost:9000
#   MINIO_BUCKET=smr-agent-events
#   MINIO_USER=minioadmin
#   MINIO_PASSWORD=minioadmin
#   SESSION_ID=verify-w1
#
# This script is intentionally a *checklist*, not a pass/fail runner — its
# steps print results inline so a human can read them. Each step exits with
# a soft warning on failure so subsequent checks still run.

set -u
BASE="${BASE:-http://localhost:8000}"
OPENSEARCH_ENDPOINT="${OPENSEARCH_ENDPOINT:-http://localhost:9200}"
OPENSEARCH_INDEX="${OPENSEARCH_INDEX:-smr-docs}"
PG_URL="${PG_URL:-postgres://agent:agent@localhost:5432/agent_state}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_BUCKET="${MINIO_BUCKET:-smr-agent-events}"
MINIO_USER="${MINIO_USER:-minioadmin}"
MINIO_PASSWORD="${MINIO_PASSWORD:-minioadmin}"
SESSION_ID="${SESSION_ID:-verify-w1-$(date +%s)}"

step() { printf "\n--- %s ---\n" "$1"; }
warn() { printf "[WARN] %s\n" "$1" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || { warn "missing tool: $1"; return 1; }; }

step "1. health endpoints"
curl -fsS "$BASE/health" || warn "agent-api /health failed"
echo
curl -fsS "$OPENSEARCH_ENDPOINT/_cluster/health" || warn "opensearch health failed"
echo

step "2. opensearch seed index document count"
curl -fsS "$OPENSEARCH_ENDPOINT/$OPENSEARCH_INDEX/_count" || warn "index _count failed"
echo

step "3. /v1/chat/completions (turn 1)"
RESP1=$(curl -fsS -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: $SESSION_ID" \
  -d '{
    "model": "agent-search-v1",
    "messages": [
      {"role": "user", "content": "i-SMR 의 피동 잔열제거계통(PRHRS) 설계 요건을 KINS 고시 기준으로 알려줘"}
    ]
  }') || warn "chat completion failed"
echo "$RESP1" | head -c 1200; echo
echo "$RESP1" | grep -q '"citations"' && echo "  citations field present" || warn "no citations field"
echo "$RESP1" | grep -q 'design-spec-pwr-smr-2025\|kins-rg-2024-001' \
  && echo "  citation references seed doc" \
  || warn "no seed doc reference in citations"

step "4. /v1/chat/completions (turn 2 — same session)"
curl -fsS -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: $SESSION_ID" \
  -d '{
    "model": "agent-search-v1",
    "messages": [
      {"role": "user", "content": "그러면 PRHRS 의 무인 잔열제거 시간은 며칠로 설계됐어?"}
    ]
  }' >/dev/null || warn "turn 2 failed"
echo "  turn 2 sent"

step "5. session_memory row for $SESSION_ID"
if need psql; then
  PGPASSWORD=agent psql "$PG_URL" -c \
    "select session_id, length(recent_turns::text) as rt_len, updated_at from session_memory where session_id='$SESSION_ID';" \
    || warn "psql query failed"
else
  warn "psql missing — open postgres via 'make psql' instead"
fi

step "6. MinIO artifacts (interaction_events / context_snapshots / prompt_render_records / tool_result_records)"
if need mc; then
  mc alias set agent "$MINIO_ENDPOINT" "$MINIO_USER" "$MINIO_PASSWORD" >/dev/null 2>&1 || true
  for prefix in interaction_events context_snapshots prompt_render_records tool_result_records; do
    n=$(mc ls --recursive "agent/$MINIO_BUCKET/" 2>/dev/null | grep -c "/$prefix/")
    printf "  %-26s : %s objects\n" "$prefix" "$n"
  done
else
  warn "mc missing — use MinIO console at $MINIO_ENDPOINT/ui (admin/$MINIO_PASSWORD)"
fi

step "7. Phoenix UI"
echo "  Open http://localhost:6006 → confirm a single trace with 15 spans"
echo "  including agent.run / classification / retrieval / llm.generation / verification / event.persist"

step "DONE — review warnings above. DoD items map to v2 worklog §W1.C1–C5."
