"""Wire protocol between the trusted worker and the agent sandbox.

The Claude agent runs in its own container (``agent_runner.py``) that holds no
credentials: no relay token, no git token, no model key (model calls go through
``model_proxy.py``).  The worker and the sandbox exchange only data over a unix
socket on a volume the two containers share:

1. ``PUT  /runs/<id>/bundle``  git bundle of the refs to work on (``refs/relay/*``)
2. ``POST /runs/<id>``         :class:`RunSpec` -> :class:`RunResult`

A result may carry a bundle of the agent's commits.  The worker imports it into
its own private clone and checks it before pushing (``repo_ops.import_commits``),
so it never runs git inside a repository the agent could have tampered with.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict

from common import TRANSIENT, RejectedError, TransientError

if TYPE_CHECKING:
    from pathlib import Path

RUNNER_SOCKET_ENV = "RELAY_AGENT_RUNNER_SOCKET"
DEFAULT_SOCKET = "/run/sandbox/runner.sock"
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_BUNDLE_BYTES = int(os.getenv("RELAY_MAX_BUNDLE_MB", "500")) * 1024 * 1024
# Longer than the sandbox's own agent timeout, so the sandbox reports it first.
RUN_TIMEOUT = int(os.getenv("AGENT_TIMEOUT_S", "1800")) + 120
RESULT_REF = "refs/relay/result"


class RunSpec(BaseModel):
    """What the sandbox should do with the uploaded bundle."""

    model_config = ConfigDict(extra="forbid")

    system: str
    prompt: str
    tools: list[str]
    checkout: str  # commit SHA from the bundle to start from
    branch: str | None = None  # create this branch at ``checkout``; None = detached
    output_format: dict[str, Any] | None = None
    # Set: commit leftovers with this message and return commits since checkout.
    commit_message: str | None = None


class RunResult(BaseModel):
    """The agent's final result message, plus its commits when requested."""

    is_error: bool
    subtype: str
    result: str = ""
    structured_output: Any = None
    api_error_status: int | None = None
    head: str | None = None  # sandbox HEAD, when commits were returned
    bundle: str | None = None  # base64 bundle ``checkout..head`` at RESULT_REF


class Sandbox:
    """Client for the agent sandbox, over its unix socket by default."""

    def __init__(
        self, socket_path: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Use ``transport`` (tests) or a unix socket at ``socket_path`` / the env default."""
        path = socket_path or os.getenv(RUNNER_SOCKET_ENV, DEFAULT_SOCKET)
        self.transport = transport or httpx.AsyncHTTPTransport(uds=path)

    async def run(self, run_id: str, bundle: Path, spec: RunSpec) -> RunResult:
        """Upload ``bundle``, run the agent on it and return the result."""
        if not RUN_ID.match(run_id):
            raise ValueError(f"bad run id {run_id!r}")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, base_url="http://sandbox", timeout=RUN_TIMEOUT
            ) as client:
                upload = await client.put(f"/runs/{run_id}/bundle", content=bundle.read_bytes())
                check(upload)
                response = await client.post(f"/runs/{run_id}", json=spec.model_dump())
                check(response)
        except httpx.HTTPError as exc:
            raise TransientError(f"sandbox unavailable: {exc}") from exc
        return RunResult.model_validate(response.json())


def check(response: httpx.Response) -> None:
    """Map a sandbox error response to ``TransientError`` / ``RejectedError``."""
    if response.is_success:
        return
    try:
        detail = str(response.json().get("detail", ""))
    except ValueError:
        detail = response.text[:500]
    if response.status_code in TRANSIENT:
        raise TransientError(f"sandbox returned {response.status_code}: {detail}")
    # The sandbox prefixes its own errors (agent_error:, git_error:).
    raise RejectedError(detail or f"sandbox_error: status {response.status_code}")
