
#!/usr/bin/env bash
# Smoke-test A2A endpoints (Docker Compose or local server).
# Usage:
#   ./scripts/a2a_smoke.sh
#   A2A_BASE=http://127.0.0.1:18800 HEALTH_BASE=http://127.0.0.1:8080 ./scripts/a2a_smoke.sh
#   SEND_MESSAGE=1 ./scripts/a2a_smoke.sh   # also POST message/send (needs LLM)

set -eo pipefail

A2A_BASE="${A2A_BASE:-http://127.0.0.1:18800}"
HEALTH_BASE="${HEALTH_BASE:-http://127.0.0.1:8080}"
SEND_MESSAGE="${SEND_MESSAGE:-0}"

echo "==> Health ${HEALTH_BASE}/live"
curl -sf "${HEALTH_BASE}/live" >/dev/null
echo "OK"

echo "==> Agent Card ${A2A_BASE}/.well-known/agent-card.json"
card_json="$(curl -sf "${A2A_BASE}/.well-known/agent-card.json")"
if command -v jq >/dev/null 2>&1; then
  echo "$card_json" | jq '{name, url, skills: (.skills | length)}'
else
  echo "$card_json" | head -c 400
  echo
fi

if [[ "$SEND_MESSAGE" == "1" ]]; then
  echo "==> JSON-RPC message/send"
  resp="$(curl -sf -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":"smoke-1","method":"message/send","params":{"message":{"messageId":"m-smoke-1","role":"user","parts":[{"kind":"text","text":"ping"}]}}}' \
    "${A2A_BASE}/")"
  if command -v jq >/dev/null 2>&1; then
    echo "$resp" | jq .
    if echo "$resp" | jq -e '.error' >/dev/null 2>&1; then
      echo "A2A message/send returned JSON-RPC error" >&2
      exit 1
    fi
  else
    echo "$resp"
    if [[ "$resp" == *'"error"'* ]]; then
      echo "A2A message/send returned JSON-RPC error" >&2
      exit 1
    fi
  fi
fi

echo "A2A smoke passed."
