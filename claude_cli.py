"""Run Claude Code in headless mode (``claude -p``) and return its result.

Every AI task in the relay goes through :func:`run_claude`: the dev-team and
reviewer agents (agent_runner.py) and free-text routing (router.py). The prompt
is sent on stdin, ``--output-format json`` makes the CLI print one result
object, and the process is killed if it runs past ``timeout``.

Authentication is whatever the ``claude`` CLI picks up from the environment:
``CLAUDE_CODE_OAUTH_TOKEN`` (a subscription token from ``claude setup-token``),
``ANTHROPIC_API_KEY``, or ``ANTHROPIC_BASE_URL`` pointing at model_proxy.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from common import TRANSIENT, AgentError, TransientError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
MODEL_TRANSIENT = TRANSIENT | {529}
MAX_STDERR = 500


@dataclass(frozen=True)
class ClaudeResult:
    """The fields of the CLI's final ``{"type": "result"}`` object the relay uses."""

    is_error: bool
    subtype: str
    result: str
    structured_output: Any = None
    api_error_status: int | None = None
    total_cost_usd: float | None = None


def available() -> bool:
    """Return True when the ``claude`` binary is on PATH."""
    return shutil.which(CLAUDE_BIN) is not None


def parse_result(stdout: str) -> ClaudeResult | None:
    """Return the last ``result`` object printed on stdout, or None."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            status = data.get("api_error_status")
            return ClaudeResult(
                is_error=bool(data.get("is_error")),
                subtype=str(data.get("subtype", "")),
                result=str(data.get("result") or ""),
                structured_output=data.get("structured_output"),
                api_error_status=status if isinstance(status, int) else None,
                total_cost_usd=data.get("total_cost_usd"),
            )
    return None


async def run_claude(
    prompt: str,
    args: Sequence[str],
    *,
    cwd: str,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> ClaudeResult:
    """Run ``claude -p --output-format json <args>`` on ``prompt``.

    Raises ``TransientError`` when the CLI is missing or the model API is
    temporarily unavailable (so the task is redelivered), and ``AgentError``
    on timeouts and runs that end without a result.
    """
    argv = [CLAUDE_BIN, "-p", "--output-format", "json", "--no-session-persistence", *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group, so a timeout kills its children too
        )
    except FileNotFoundError as exc:
        raise TransientError(f"claude CLI not found ({CLAUDE_BIN})") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise AgentError(f"timed out after {timeout:.0f}s") from exc

    # A failed run (max turns, budget, API error) still prints its result object.
    result = parse_result(stdout.decode(errors="replace"))
    if result is None:
        tail = stderr.decode(errors="replace").strip()[-MAX_STDERR:]
        raise AgentError(f"claude exited {proc.returncode} without a result: {tail}")
    if result.is_error and result.api_error_status in MODEL_TRANSIENT:
        raise TransientError(f"model API returned {result.api_error_status}")
    return result
