"""Task handlers a worker can run, selected with ``--handler``.

A handler receives the claim and the worker's authenticated HTTP client and
returns ``("complete", output)`` or ``("fail", error)``.  Raising
``TransientError`` instead leaves the task unacknowledged so its lease expires
and it is redelivered, which is the right outcome for transient errors.

Task input is untrusted data and is stored in the relay DB and shown on the
dashboard, so handlers never put credential values into task input or output.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import anthropic
import httpx

from agent_roles import make_dev_team, make_reviewer
from common import (
    HTTP_CREATED,
    TRANSIENT,
    Handler,
    Outcome,
    RejectedError,
    TransientError,
    is_json,
    load_request,
    lookup_project,
    rejecting,
    require_credential,
    require_pr,
    require_ref,
    require_task,
)
from projects import ProjectConfig, load_registry
from router import interpret

HANDLERS = ("uppercase", "orchestrator", "deployer", "dev-team", "reviewer")

# Which worker role carries out each orchestrator action.
ACTION_ROLES = {"build": "dev-team", "review": "reviewer", "deploy": "deployer"}


def role_env_var(role: str) -> str:
    """Environment variable naming the agent that serves ``role``."""
    return "RELAY_ROLE_AGENT_" + re.sub(r"[^A-Z0-9]", "_", role.upper())


async def uppercase(claim: dict[str, Any], _client: httpx.AsyncClient) -> Outcome:
    """Demo handler: echo the input in upper case."""
    # Keep it explicit so task input remains untrusted data.
    return "complete", claim["input"].upper()


# ---------------------------------------------------------------- orchestrator


def action_extras(action: str, request: dict[str, Any]) -> dict[str, Any]:
    """Validate and return the action-specific fields forwarded to the child task."""
    if action == "build":
        return {"task": require_task(request)}
    if action == "review":
        return {"pr": require_pr(request)}
    return {}


async def dispatch(client: httpx.AsyncClient, recipient: str, child_input: str, parent: str) -> str:
    """Create the child task on the relay and return its id."""
    # Per-parent idempotency key: a redelivered orchestrator task returns the
    # existing child instead of a duplicate.
    try:
        response = await client.post(
            "/api/v1/tasks",
            json={"to": recipient, "input": child_input},
            headers={"Idempotency-Key": f"orch:{parent}"},
        )
    except httpx.HTTPError as exc:
        raise TransientError(f"dispatch failed: {exc}") from exc
    if response.status_code in TRANSIENT:
        raise TransientError(f"dispatch got {response.status_code}")
    if response.status_code != HTTP_CREATED:
        raise RejectedError(f"dispatch_rejected: relay returned {response.status_code}")
    return str(response.json()["task_id"])


def make_orchestrator(
    projects: dict[str, ProjectConfig],
    role_agents: dict[str, str],
    llm: anthropic.AsyncAnthropic | None = None,
) -> Handler:
    """Build the handler that validates a request and dispatches it to a role.

    With ``llm``, non-JSON input is interpreted as a free-text request;
    without it, only JSON input is accepted.
    """

    async def read_request(raw: str) -> tuple[dict[str, Any], bool]:
        # JSON keeps strict parsing; only non-JSON text goes through the LLM.
        if llm is None or is_json(raw):
            return load_request(raw), False
        return await interpret(raw, projects, ACTION_ROLES, llm), True

    @rejecting
    async def orchestrate(claim: dict[str, Any], client: httpx.AsyncClient) -> Outcome:
        request, interpreted = await read_request(claim["input"])
        key = request["project"]
        project = lookup_project(projects, key)
        action = request.get("action", "deploy")
        role = ACTION_ROLES.get(action) if isinstance(action, str) else None
        if role is None:
            raise RejectedError(f"invalid_input: action must be one of {sorted(ACTION_ROLES)}")
        ref = require_ref(request, project)
        extras = action_extras(action, request)
        recipient = role_agents.get(role)
        if not recipient:
            raise RejectedError(
                f"no_agent: role {role!r} has no registered agent ({role_env_var(role)} unset)"
            )

        # Deterministic input so a redelivered task produces the same child.
        child_input = json.dumps(
            {
                **project.public_view(key),
                **extras,
                "action": action,
                "ref": ref,
                "parent_task": claim["task_id"],
            },
            sort_keys=True,
        )
        child_id = await dispatch(client, recipient, child_input, claim["task_id"])
        return "complete", json.dumps(
            {
                "project": key,
                "action": action,
                "ref": ref,
                "dispatched_to": role,
                "child_task_id": child_id,
                "interpreted_from_text": interpreted,
            },
            sort_keys=True,
        )

    return orchestrate


# ---------------------------------------------------------------- deployer


def make_deployer(projects: dict[str, ProjectConfig], secrets_dir: str | None = None) -> Handler:
    """Build the handler that deploys a project's ref to its registered target."""

    @rejecting
    async def deploy(claim: dict[str, Any], _client: httpx.AsyncClient) -> Outcome:
        request = load_request(claim["input"])
        key = request["project"]
        # The registry, not the task input, is the source of truth for where
        # and with which credentials a project is deployed.
        project = lookup_project(projects, key)
        ref = require_ref(request, project)
        require_credential(project, secrets_dir)
        target = f"{project.deploy_target.kind}/{project.deploy_target.name}"
        # Placeholder for the real deploy (helm upgrade / vercel deploy) that
        # would use the mounted credential. Never echo the credential.
        return "complete", json.dumps(
            {
                "project": key,
                "deploy_target": target,
                "image": f"{project.registry}:{ref}",
                "status": "simulated",
            },
            sort_keys=True,
        )

    return deploy


# ---------------------------------------------------------------- selection


def router_llm() -> anthropic.AsyncAnthropic | None:
    """LLM client for free-text routing, or None when no API key is configured."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    return anthropic.AsyncAnthropic()


def build_handler(name: str) -> Handler:
    """Return the handler selected with ``--handler``."""
    if name == "uppercase":
        return uppercase
    projects = load_registry()
    if name == "orchestrator":
        role_agents = {role: os.getenv(role_env_var(role), "") for role in ACTION_ROLES.values()}
        return make_orchestrator(projects, role_agents, router_llm())
    if name == "deployer":
        return make_deployer(projects)
    if name == "dev-team":
        return make_dev_team(projects)
    if name == "reviewer":
        return make_reviewer(projects)
    raise SystemExit(f"unknown handler {name!r}; choose from {HANDLERS}")


__all__ = [
    "ACTION_ROLES",
    "HANDLERS",
    "Handler",
    "TransientError",
    "build_handler",
    "make_deployer",
    "make_orchestrator",
    "uppercase",
]
