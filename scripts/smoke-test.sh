#!/usr/bin/env bash
# Register a sender, send COUNT tasks to each role in ROLES, and wait for all
# of them to complete with uppercased output. Prints which pods did the work.
set -euo pipefail

NS="${NS:-agent-relay}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
# Uppercase roles come from values.yaml (ConfigMap relay-roles); orchestrator
# and deployer take JSON project requests and are covered by smoke-project.sh.
ROLES="${ROLES:-$(kubectl -n "$NS" get configmap relay-roles -o jsonpath='{.data.uppercase_roles}')}"
if [[ -z "$ROLES" ]]; then
  echo "no uppercase roles in values.yaml; test project roles with 'make smoke-project'"
  exit 0
fi
COUNT="${COUNT:-3}"
TIMEOUT="${TIMEOUT:-60}"

json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
secret_value() {
  kubectl -n "$NS" get secret "$1" -o jsonpath="{.data.$2}" | base64 -d
}

ENROLLMENT_SECRET="$(secret_value relay-enrollment RELAY_ENROLLMENT_SECRET)"

sender_token="$(curl -fsS -X POST "$BASE_URL/api/v1/agents" \
  -H 'content-type: application/json' \
  -H "X-Enrollment-Secret: $ENROLLMENT_SECRET" \
  -d '{"name":"smoke-sender"}' | json '["token"]')"
auth=(-H "Authorization: Bearer $sender_token")

task_ids=()
expected=()
for role in $ROLES; do
  agent="$(secret_value "worker-$role" AGENT_ID)"
  for i in $(seq 1 "$COUNT"); do
    input="hello $role $i"
    id="$(curl -fsS -X POST "$BASE_URL/api/v1/tasks" "${auth[@]}" \
      -H 'content-type: application/json' \
      -H "Idempotency-Key: smoke-$$-$role-$i" \
      -d "{\"to\":\"$agent\",\"input\":\"$input\"}" | json '["task_id"]')"
    task_ids+=("$id")
    expected+=("${input^^}")
  done
  echo "sent $COUNT tasks to $role ($agent)"
done

deadline=$(( $(date +%s) + TIMEOUT ))
for i in "${!task_ids[@]}"; do
  id="${task_ids[$i]}"
  while :; do
    task="$(curl -fsS "$BASE_URL/api/v1/tasks/$id" "${auth[@]}")"
    status="$(json '["status"]' <<<"$task")"
    if [[ "$status" == "completed" ]]; then
      output="$(json '["output"]' <<<"$task")"
      [[ "$output" == "${expected[$i]}" ]] || { echo "FAIL $id: got '$output', want '${expected[$i]}'"; exit 1; }
      break
    fi
    [[ "$status" == "failed" ]] && { echo "FAIL $id: task failed"; exit 1; }
    (( $(date +%s) < deadline )) || { echo "FAIL $id: still '$status' after ${TIMEOUT}s"; exit 1; }
    sleep 1
  done
done
echo "PASS: all ${#task_ids[@]} tasks completed"

echo "completions per worker pod:"
kubectl -n "$NS" logs -l app=agent-relay-worker --prefix --tail=-1 --max-log-requests=20 2>/dev/null \
  | grep -o 'worker=[^ ]* .*status=completed' | awk '{print $1}' | sort | uniq -c || true
