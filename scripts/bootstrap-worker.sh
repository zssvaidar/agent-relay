#!/usr/bin/env bash
# Register one worker role as an agent and store its credentials in
# Secret worker-<role>.  Usage: bootstrap-worker.sh <role>
# Idempotent: if the Secret already exists, nothing is registered.
set -euo pipefail

ROLE="${1:?usage: $0 <role>   e.g. $0 reviewer}"
NS="${NS:-agent-relay}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
SECRET="worker-$ROLE"

if kubectl -n "$NS" get secret "$SECRET" >/dev/null 2>&1; then
  echo "secret/$SECRET already exists; skipping registration of '$ROLE'"
  exit 0
fi

kubectl -n "$NS" rollout status deploy/agent-relay-web --timeout=120s

ENROLLMENT_SECRET="$(kubectl -n "$NS" get secret relay-enrollment \
  -o jsonpath='{.data.RELAY_ENROLLMENT_SECRET}' | base64 -d)"

response="$(curl -fsS -X POST "$BASE_URL/api/v1/agents" \
  -H 'content-type: application/json' \
  -H "X-Enrollment-Secret: $ENROLLMENT_SECRET" \
  -d "{\"name\":\"$ROLE\",\"description\":\"$ROLE worker (kind)\"}")"

agent_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["agent_id"])' <<<"$response")"
token="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' <<<"$response")"

kubectl -n "$NS" create secret generic "$SECRET" \
  --from-literal=AGENT_ID="$agent_id" \
  --from-literal=TOKEN="$token"
kubectl -n "$NS" label secret "$SECRET" app=agent-relay-worker role="$ROLE" >/dev/null

echo "registered '$ROLE' as $agent_id -> secret/$SECRET"
