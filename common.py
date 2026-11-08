"""Shared types and input validation for relay task handlers.

A handler receives the claim and the worker's authenticated HTTP client and
returns ``("complete", output)`` or ``("fail", error)``.  Raising
``TransientError`` leaves the task unacknowledged so its lease expires and it
is redelivered.  Raising ``RejectedError`` inside a handler wrapped with
``@rejecting`` fails the task with the exception's message.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx

from projects import ProjectConfig, read_credential

Outcome = tuple[Literal["complete", "fail"], str]
Handler = Callable[[dict[str, Any], httpx.AsyncClient], Awaitable[Outcome]]

GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
TRANSIENT = frozenset({429, 500, 502, 503, 504})
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_UNPROCESSABLE = 422
MAX_TASK_TEXT = 8000


class TransientError(RuntimeError):
    """Leave the task for redelivery."""


class RejectedError(ValueError):
    """The task cannot be carried out; it fails with this message."""


class AgentError(RejectedError):
    """The agent run failed in a way retrying will not fix."""

    def __init__(self, message: str) -> None:
        """Prefix the message so the dashboard shows where it failed."""
        super().__init__(f"agent_error: {message}")


def rejecting(handler: Handler) -> Handler:
    """Turn a ``RejectedError`` raised by ``handler`` into a ``("fail", message)`` outcome."""

    @functools.wraps(handler)
    async def wrapper(claim: dict[str, Any], client: httpx.AsyncClient) -> Outcome:
        try:
            return await handler(claim, client)
        except RejectedError as exc:
            return "fail", str(exc)

    return wrapper


def valid_ref(ref: object) -> bool:
    """Return True for a plausible branch, tag or commit name."""
    return isinstance(ref, str) and bool(GIT_REF.match(ref)) and ".." not in ref


def is_json(raw: str) -> bool:
    """Return True when ``raw`` parses as JSON."""
    try:
        json.loads(raw)
    except ValueError:
        return False
    return True


def parse_request(raw: str) -> dict[str, Any] | str:
    """Parse JSON task input; return an error string when it is unusable."""
    try:
        data = json.loads(raw)
    except ValueError:
        return 'invalid_input: expected JSON like {"project": "<key>", "action": "deploy"}'
    if not isinstance(data, dict) or not isinstance(data.get("project"), str):
        return "invalid_input: 'project' (string) is required"
    return data


def load_request(raw: str) -> dict[str, Any]:
    """Parse JSON task input or raise ``RejectedError``."""
    request = parse_request(raw)
    if isinstance(request, str):
        raise RejectedError(request)
    return request


def lookup_project(projects: dict[str, ProjectConfig], key: str) -> ProjectConfig:
    """Return the registry entry for ``key`` or raise ``RejectedError``."""
    project = projects.get(key)
    if project is None:
        raise RejectedError(f"unknown_project: {key!r} is not in the registry")
    return project


def require_ref(request: dict[str, Any], project: ProjectConfig) -> str:
    """Return the request's ref (default branch if absent) or raise ``RejectedError``."""
    ref = request.get("ref", project.default_branch)
    if not valid_ref(ref):
        raise RejectedError("invalid_input: ref must be a branch, tag or commit")
    return str(ref)


def require_task(request: dict[str, Any]) -> str:
    """Return the build request's task description, trimmed, or raise ``RejectedError``."""
    task = request.get("task")
    if not isinstance(task, str) or not task.strip():
        raise RejectedError("invalid_input: build needs 'task' describing the change")
    return task.strip()[:MAX_TASK_TEXT]


def require_pr(request: dict[str, Any]) -> int:
    """Return the review request's pull request number or raise ``RejectedError``."""
    pr = request.get("pr")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise RejectedError("invalid_input: review needs 'pr' (pull request number)")
    return pr


def require_credential(project: ProjectConfig, secrets_dir: str | None) -> str:
    """Return the project's mounted credential or raise ``RejectedError``."""
    token = read_credential(project, secrets_dir)
    if token is None:
        raise RejectedError(
            f"credentials_unavailable: secret {project.secret_name!r} is not mounted"
        )
    return token
