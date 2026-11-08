#!/usr/bin/env bash
# Send {"project": PROJECT, "action": ACTION} to the orchestrator, then follow
# the child task it dispatches (e.g. to the deployer).
# Usage: PROJECT=cp-expiry-tracker REF=main ./scripts/smoke-project.sh
set -euo pipefail

NS="${NS:-agent-relay}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
PROJECT="${PROJECT:?set PROJECT=<registry key>}"
ACTION="${ACTION:-deploy}"
REF="${REF:-}"
TASK="${TASK:-}"   # for ACTION=build
PR="${PR:-}"       # for ACTION=review
TIMEOUT="${TIMEOUT:-60}"

json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
result() { python3 -c 'import json,sys; t=json.load(sys.stdin); print(t["status"], t["output"] or t["error"])'; }
secret_value() { kubectl -n "$NS" get secret "$1" -o jsonpath="{.data.$2}" | base64 -d; }

ENROLLMENT_SECRET="$(secret_value relay-enrollment RELAY_ENROLLMENT_SECRET)"
sender_token="$(curl -fsS -X POST "$BASE_URL/api/v1/agents" \
  -H 'content-type: application/json' -H "X-Enrollment-Secret: $ENROLLMENT_SECRET" \
  -d '{"name":"smoke-project"}' | json '["token"]')"
orchestrator="$(secret_value worker-orchestrator AGENT_ID)"

body="$(PROJECT="$PROJECT" ACTION="$ACTION" REF="$REF" TASK="$TASK" PR="$PR" TO="$orchestrator" python3 -c '
import json, os
r = {"project": os.environ["PROJECT"], "action": os.environ["ACTION"]}
if os.environ["REF"]:
    r["ref"] = os.environ["REF"]
if os.environ["TASK"]:
    r["task"] = os.environ["TASK"]
if os.environ["PR"]:
    r["pr"] = int(os.environ["PR"])
print(json.dumps({"to": os.environ["TO"], "input": json.dumps(r)}))')"
task_id="$(curl -fsS -X POST "$BASE_URL/api/v1/tasks" -H "Authorization: Bearer $sender_token" \
  -H 'content-type: application/json' -d "$body" | json '["task_id"]')"
echo "sent to orchestrator -> $task_id"

wait_task() {  # <task_id> <bearer token>; prints the final task JSON
  local deadline=$(( $(date +%s) + TIMEOUT )) task status
  while :; do
    task="$(curl -fsS "$BASE_URL/api/v1/tasks/$1" -H "Authorization: Bearer $2")"
    status="$(json '["status"]' <<<"$task")"
    if [[ "$status" == completed || "$status" == failed ]]; then echo "$task"; return; fi
    (( $(date +%s) < deadline )) || { echo "FAIL $1 still '$status' after ${TIMEOUT}s" >&2; exit 1; }
    sleep 1
  done
}

parent="$(wait_task "$task_id" "$sender_token")"
echo "orchestrator: $(result <<<"$parent")"
[[ "$(json '["status"]' <<<"$parent")" == completed ]] || exit 1

output="$(json '["output"]' <<<"$parent")"
child_id="$(json '["child_task_id"]' <<<"$output")"
role="$(json '["dispatched_to"]' <<<"$output")"
# Only participants can read a task; the orchestrator is the child's sender.
child="$(wait_task "$child_id" "$(secret_value worker-orchestrator TOKEN)")"
echo "$role: $(result <<<"$child")"
[[ "$(json '["status"]' <<<"$child")" == completed ]] && echo PASS || { echo FAIL; exit 1; }
