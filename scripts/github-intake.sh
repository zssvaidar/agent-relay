#!/usr/bin/env bash
# Wire a GitHub repo's issues to the orchestrator.
#   REPO=zssvaidar/cp-expiry-tracker PROJECT=cp-expiry-tracker ./scripts/github-intake.sh
#
# 1. Registers a relay sender identity "github-intake" once and keeps its
#    credentials in Secret relay-github-intake (idempotent).
# 2. Sets the repo's Actions secret/variables the intake workflow reads.
# 3. Creates the `agent:build` label.
# The workflow file itself must be committed to the repo by hand.
set -euo pipefail

NS="${NS:-agent-relay}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
REPO="${REPO:?set REPO=<owner/name>}"
PROJECT="${PROJECT:?set PROJECT=<registry key>}"
# Address of the relay as seen from the ARC runner pod (in-cluster).
RELAY_URL="${RELAY_URL:-http://agent-relay-web.$NS.svc.cluster.local:8000}"
RUNNER="${RUNNER:-}"
SECRET=relay-github-intake
LABEL=agent:build

secret_value() { kubectl -n "$NS" get secret "$1" -o jsonpath="{.data.$2}" | base64 -d; }

command -v gh >/dev/null || { echo "gh CLI is required (https://cli.github.com)"; exit 1; }
uv run python projects.py chart/agent-relay/values.yaml "$PROJECT" >/dev/null \
  || { echo "project '$PROJECT' is not in the registry"; exit 1; }
orchestrator="$(secret_value worker-orchestrator AGENT_ID)" \
  || { echo "secret/worker-orchestrator missing; run make deploy first"; exit 1; }

if ! kubectl -n "$NS" get secret "$SECRET" >/dev/null 2>&1; then
  enrollment="$(secret_value relay-enrollment RELAY_ENROLLMENT_SECRET)"
  response="$(curl -fsS -X POST "$BASE_URL/api/v1/agents" \
    -H 'content-type: application/json' -H "X-Enrollment-Secret: $enrollment" \
    -d '{"name":"github-intake","description":"Submits labeled GitHub issues to the orchestrator"}')"
  agent_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["agent_id"])' <<<"$response")"
  token="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' <<<"$response")"
  kubectl -n "$NS" create secret generic "$SECRET" \
    --from-literal=AGENT_ID="$agent_id" --from-literal=TOKEN="$token" >/dev/null
  kubectl -n "$NS" label secret "$SECRET" app=agent-relay-intake >/dev/null
  echo "registered github-intake as $agent_id -> secret/$SECRET"
fi

# The token goes over stdin so it never appears in the process list.
secret_value "$SECRET" TOKEN | gh secret set RELAY_TOKEN -R "$REPO"
gh variable set RELAY_URL -R "$REPO" --body "$RELAY_URL"
gh variable set RELAY_ORCHESTRATOR_ID -R "$REPO" --body "$orchestrator"
gh variable set RELAY_PROJECT -R "$REPO" --body "$PROJECT"
[ -z "$RUNNER" ] || gh variable set RELAY_RUNNER -R "$REPO" --body "$RUNNER"
gh label create "$LABEL" -R "$REPO" --force --color 5319e7 \
  --description "Send this issue to the agent relay as a build task" >/dev/null

cat <<EOF
configured $REPO -> project '$PROJECT' (orchestrator $orchestrator, relay $RELAY_URL)

Remaining steps:
  1. Commit integrations/github/relay-issue-intake.yml to $REPO as
     .github/workflows/relay-issue-intake.yml
  2. Register an ARC runner set for that repo (runner sets are per repo):
       GITHUB_TOKEN=<pat> make arc-runners GITHUB_CONFIG_URL=https://github.com/$REPO \\
         ARC_RUNNER_SET=${RUNNER:-agent-relay-runner}
     If agent-relay-runner already serves another repo, pick a different
     ARC_RUNNER_SET and rerun this script with RUNNER=<that name>.
  3. Label an issue '$LABEL'.
EOF
