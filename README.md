# Agent Relay (SQLite starter)

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. The local starter is self-contained:
SQLite persists the queue and attempts, while workers execute tasks on their own
machines. The included worker deterministically returns `input.upper()`.

## Run it

```bash
uv sync
uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. The default
database is `./agent-relay.db`; set `RELAY_DATABASE_URL` to use another SQLite
file. `GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so a wiped volume
reports not-ready instead of passing with zero tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Project registry (orchestrator)

The projects the agents may work on live in the `projects:` block of
`chart/agent-relay/values.yaml`. The chart renders the block into ConfigMap
`relay-projects`, and `projects.py` validates it:

```yaml
projects:
  cp-expiry-tracker:
    repo: git@github.com:zssvaidar/cp-expiry-tracker.git
    default_branch: main
    registry: ghcr.io/zssvaidar/cp-expiry-tracker
    deploy_target: k8s/cp-expiry-tracker     # <k8s|vercel>/<name>
    credentials: secret/cp-expiry-tracker-bot  # a Secret NAME, never a value
```

- The **orchestrator** worker (`--handler orchestrator`) reads the registry.
  Send it `{"project": "<key>", "action": "build|review|deploy", "ref": "main"}`.
  It looks the project up and sends one child task to `dev-team`, `reviewer` or
  `deployer` (idempotency key `orch:<task_id>`, so a redelivered task creates no
  duplicate). Unknown projects and malformed requests fail with a clear error.
- The **deployer** (`--handler deployer`) is the only pod that mounts project
  credentials, read-only, at `/var/run/secrets/projects/<secret>/token`. If the
  Secret is missing, the deployer fails that project's tasks with
  `credentials_unavailable`. The deploy itself is simulated for now.
- Task input and output only ever contain the project key and public fields.
  They are stored in the DB and shown on the dashboard.

```bash
make check-projects                                   # validate + check Secrets exist
make project-secret PROJECT=cp-expiry-tracker FROM=./token.txt
make smoke-project PROJECT=cp-expiry-tracker REF=main # orchestrator -> deployer
```

To add a project: add an entry under `projects:`, run `make project-secret`, then
`make deploy`. The orchestrator and deployer restart on registry changes.

## Agent roles (dev-team, reviewer)

`dev-team` and `reviewer` run a Claude agent (`claude-agent-sdk`) on the
project's repo. The orchestrator dispatches to them:

```json
{"project": "cp-expiry-tracker", "action": "build", "task": "Add a /health endpoint"}
{"project": "cp-expiry-tracker", "action": "review", "pr": 12}
```

The orchestrator also takes plain-text requests ("review PR 12 on
cp-expiry-tracker"). When `ANTHROPIC_API_KEY` is set, it uses a model to map
the text onto the same JSON, choosing only registered projects and known
actions. If the project or action is unclear, the task fails with
`needs_clarification`.

### Pod layout: the agent holds no credentials

| container     | holds                                     | does                                           |
|---------------|-------------------------------------------|------------------------------------------------|
| `worker`      | relay token, project registry + git token | claims tasks, clones, pushes, GitHub API       |
| `agent`       | nothing                                   | runs the agent ([agent_runner.py](agent_runner.py)) |
| `model-proxy` | `ANTHROPIC_API_KEY`                       | forwards `/v1/*` to the API and adds the key ([model_proxy.py](model_proxy.py)) |

Containers in a pod don't share a process namespace or Secret mounts, so the
agent can't read the other containers' environment or files. Worker pods also
don't mount a Kubernetes service-account token. Code moves only as data, over a
unix socket ([sandbox.py](sandbox.py)):

1. The worker clones into its private filesystem and sends a git bundle.
2. The agent works on a clone of that bundle. The clone has no remote.
3. The sandbox returns the result and a bundle of the agent's commits. The
   worker imports them into its own clone with `fetch.fsckObjects` on, checks
   that they build on the requested ref, then pushes `agent/<task_id>` and
   opens the PR.

The worker never runs git in a repository the agent touched, so the agent's
`.git/config` and hooks can't run with the token.

- **dev-team** creates branch `agent/<task_id>`. Branch names are
  deterministic, so a redelivered task reuses the branch and PR.
- **reviewer** reviews the PR head, or the commit given as `ref`. The worker
  posts the agent's structured verdict as a GitHub review. If posting fails,
  the verdict stays in the task output (`post_error`). GitHub won't let an
  account review its own PR, so give the reviewer a different token from
  dev-team.
- Limits, set in `sandbox.agentEnv`: `AGENT_MAX_TURNS` (60),
  `AGENT_MAX_BUDGET_USD` (5), `AGENT_TIMEOUT_S` (1800), `AGENT_MODEL`.

**What this doesn't cover.** The agent can still use the network: it can
call the model through the proxy beyond its budget, or send repo contents out.
Add a NetworkPolicy for the pod if that matters. Its output also still drives
the worker's actions: commits get pushed, and verdicts get posted. Protect
default branches on GitHub and require a human approval (CODEOWNERS), so an
agent PR or an agent approval alone can never merge.

Run locally (three terminals, plus a worker with `RELAY_PROJECTS_FILE`):

```bash
ANTHROPIC_API_KEY=... uv run uvicorn model_proxy:app --port 8787
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY=x \
  RELAY_AGENT_RUNNER_SOCKET=/tmp/runner.sock uv run python agent_runner.py
RELAY_AGENT_RUNNER_SOCKET=/tmp/runner.sock uv run python main.py worker --handler dev-team ...
```

In the cluster:

```bash
make llm-secret FROM=./anthropic-key.txt   # Secret relay-anthropic (ANTHROPIC_API_KEY)
make smoke-project PROJECT=cp-expiry-tracker ACTION=build TASK="Add a /health endpoint" TIMEOUT=1800
make smoke-project PROJECT=cp-expiry-tracker ACTION=review PR=12 TIMEOUT=1800
```

## Storage and delivery behavior

`database.py` contains SQLAlchemy models, SQLite WAL setup, and the isolated
`BEGIN IMMEDIATE` transaction helper. `storage.py` contains task/claim/recovery
operations; routes and request models are kept in `main.py` and `schemas.py`.
SQLite does not provide PostgreSQL's `FOR UPDATE SKIP LOCKED`, so the starter
serializes writer transactions to make concurrent claims safe across processes.
Students can port this storage seam to PostgreSQL later without changing the
HTTP protocol or lifecycle in `SPEC.md`.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q
```

Tests default to a scratch database at `/tmp/agent-relay-test.db` so they
don't reset your dev server's `./agent-relay.db`. The fixture drops and
recreates all tables on whatever `RELAY_DATABASE_URL` points at, so stop
the dev server first or set `RELAY_DATABASE_URL` to a scratch file before
running tests against another database.

This starter intentionally does not include Docker, Kubernetes, CI, external
brokers, an LLM, or a PostgreSQL implementation. Those are deployment and
student-port concerns rather than part of the local relay protocol.
