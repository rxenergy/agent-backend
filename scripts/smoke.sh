#!/bin/sh
# Send one query to the local agent-api and print the response.
set -e
BASE="${BASE:-http://localhost:8000}"

curl -fsS "$BASE/health"
echo

curl -fsS -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agent-search-v1",
    "messages": [
      {"role": "user", "content": "APR1400과 i-SMR 안전계통 차이를 알려줘"}
    ]
  }'
echo
