#!/bin/sh
# Stream a sample query to the local agent-api and print each SSE frame on
# its own line — quick eyeball check that token / step / tool events are
# arriving incrementally rather than as one trailing blob.
#
# Requirements:
#   - `make up-local` is running (agent-api on $BASE).
#   - `curl` (with -N for unbuffered output) and `jq` for pretty-printing
#     each `data: {...}` payload. If jq is missing the raw line is shown.
#
# Usage:
#   scripts/smoke-stream.sh
#   BASE=http://other-host:8000 MODEL=agentic_finder_v4@gemma-stream \
#     scripts/smoke-stream.sh
set -eu

BASE="${BASE:-http://localhost:8000}"
MODEL="${MODEL:-agentic_finder_v4@fake-echo}"
QUERY="${QUERY:-APR1400과 i-SMR 안전계통 차이를 알려줘}"

have_jq=0
if command -v jq >/dev/null 2>&1; then
  have_jq=1
fi

curl -fsS "$BASE/health"
echo

# -N: disable curl output buffering so SSE frames render as they arrive.
curl -fsSN -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "X-Session-Id: smoke-stream-$$" \
  -d "{
    \"model\": \"$MODEL\",
    \"stream\": true,
    \"messages\": [{\"role\": \"user\", \"content\": \"$QUERY\"}]
  }" | while IFS= read -r line; do
  case "$line" in
    "data: [DONE]")
      printf '%s\n' "$line"
      ;;
    data:*)
      payload=${line#data: }
      if [ "$have_jq" -eq 1 ]; then
        printf '%s\n' "$payload" | jq -c '{
          kind: (
            .choices[0].finish_reason // (
              if (.choices[0].delta.content // "") != "" then "token"
              elif (.choices[0].delta.reasoning_content // "") != "" then "reasoning"
              elif (.smr_agent.event // null) != null then "event"
              elif (.choices[0].delta.role // "") != "" then "open"
              else "other" end
            )
          ),
          delta: .choices[0].delta,
          event: .smr_agent.event,
          finish: .choices[0].finish_reason,
          usage: .usage
        }'
      else
        printf '%s\n' "$payload"
      fi
      ;;
    "")
      ;;
    *)
      printf '%s\n' "$line"
      ;;
  esac
done
