"""Agent sandbox: runs Claude Code headlessly on a git bundle, holding no credentials.

Runs as its own container in the dev-team / reviewer pods and listens on a
unix socket (see sandbox.py for the protocol)::

    python agent_runner.py        # socket at $RELAY_AGENT_RUNNER_SOCKET

Each run is one ``claude -p`` process (claude_cli.py) in the task's clone.
Everything in this container is treated as reachable by the agent: it has no
relay, git or model credential (``ANTHROPIC_BASE_URL`` points at the pod's
model proxy), and its process namespace is separate from the worker's.

Defence in depth inside the sandbox: an explicit tool list
(``--permission-mode dontAsk`` denies the rest), a PreToolUse command hook
(agent_guard.py) keeping file tools in the workspace, and no settings or hooks
loaded from the repository (``--setting-sources ""``); its AGENTS.md /
CLAUDE.md is read as plain text into the system prompt instead.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import claude_cli
from agent_guard import ROOT_ENV
from common import AgentError, RejectedError, TransientError
from repo_ops import BOT_EMAIL, BOT_NAME, git
from sandbox import (
    DEFAULT_SOCKET,
    MAX_BUNDLE_BYTES,
    RESULT_REF,
    RUN_ID,
    RUNNER_SOCKET_ENV,
    RunResult,
    RunSpec,
)

AGENT_MODEL = os.getenv("AGENT_MODEL") or None  # None = CLI default
AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT_S", "1800"))
AGENT_MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "60"))
AGENT_MAX_BUDGET_USD = float(os.getenv("AGENT_MAX_BUDGET_USD", "5"))
WORK_ROOT = os.getenv("AGENT_WORK_ROOT", tempfile.gettempdir())
MAX_INSTRUCTIONS = 20000
GUARD_SCRIPT = Path(__file__).with_name("agent_guard.py")

app = FastAPI(title="Agent sandbox")


@app.exception_handler(RejectedError)
async def rejected(_request: Request, exc: RejectedError) -> JSONResponse:
    """Errors the task fails with (agent_error:, git_error:)."""
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(TransientError)
async def transient(_request: Request, exc: TransientError) -> JSONResponse:
    """Errors the worker leaves for redelivery."""
    return JSONResponse({"detail": str(exc)}, status_code=503)


# ---------------------------------------------------------------- the agent run


def guard_settings() -> str:
    """``--settings`` JSON registering agent_guard.py as the PreToolUse hook."""
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(GUARD_SCRIPT))}"
    return json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": command}]}]}})


def repo_instructions(workdir: Path) -> str:
    """Contents of the repo's AGENTS.md / CLAUDE.md, read as plain text."""
    parts = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = workdir / name
        if path.is_file():
            text = path.read_text(errors="replace")[:MAX_INSTRUCTIONS]
            parts.append(f"--- {name} ---\n{text}")
    return "\n\n".join(parts)


def agent_args(spec: RunSpec, workdir: Path) -> list[str]:
    """Locked-down ``claude -p`` flags for one agent run."""
    tools = ",".join(spec.tools)
    args = [
        "--system-prompt", f"{spec.system}\n\n{repo_instructions(workdir)}".rstrip(),
        "--tools", tools,  # the only built-in tools that exist in this run
        "--allowedTools", tools,
        "--disallowedTools", "WebFetch,WebSearch",
        "--permission-mode", "dontAsk",  # anything not allowed is denied
        "--setting-sources", "",  # don't load settings/hooks from the repo or $HOME
        "--strict-mcp-config",  # and no MCP servers
        "--settings", guard_settings(),
        "--max-turns", str(AGENT_MAX_TURNS),
        "--max-budget-usd", str(AGENT_MAX_BUDGET_USD),
    ]
    if AGENT_MODEL:
        args += ["--model", AGENT_MODEL]
    if spec.output_format:
        args += ["--json-schema", json.dumps(spec.output_format["schema"])]
    return args


async def run_agent(spec: RunSpec, workdir: Path) -> claude_cli.ClaudeResult:
    """Run Claude Code headlessly in ``workdir`` and return its result."""
    root = str(workdir.resolve())
    return await claude_cli.run_claude(
        spec.prompt,
        agent_args(spec, workdir),
        cwd=root,
        timeout=AGENT_TIMEOUT,
        env={ROOT_ENV: root},
    )


# ---------------------------------------------------------------- the repository


def run_dir(run_id: str) -> Path:
    """Directory holding one run's bundle and clone."""
    if not RUN_ID.match(run_id):
        raise HTTPException(status_code=400, detail="invalid run id")
    return Path(WORK_ROOT) / "runs" / run_id


async def checkout(bundle: Path, repo: Path, spec: RunSpec) -> None:
    """Create ``repo`` from the bundle's ``refs/relay/*`` and check out the start commit."""
    repo.mkdir(parents=True)
    await git("init", "-q", cwd=repo)
    await git("fetch", "-q", "--no-tags", str(bundle), "refs/relay/*:refs/relay/*", cwd=repo)
    await git("config", "user.name", BOT_NAME, cwd=repo)
    await git("config", "user.email", BOT_EMAIL, cwd=repo)
    if spec.branch:
        await git("checkout", "-q", "-B", spec.branch, spec.checkout, cwd=repo)
    else:
        await git("checkout", "-q", "--detach", spec.checkout, cwd=repo)


async def collect_commits(repo: Path, spec: RunSpec, message: str) -> tuple[str, str] | None:
    """Commit leftovers; return ``(head, base64 bundle)`` of new commits, or None."""
    if await git("status", "--porcelain", cwd=repo):
        await git("add", "-A", cwd=repo)
        await git("commit", "-q", "-m", message, cwd=repo)
    if await git("rev-list", "--count", f"{spec.checkout}..HEAD", cwd=repo) == "0":
        return None
    head = await git("rev-parse", "HEAD", cwd=repo)
    await git("update-ref", RESULT_REF, head, cwd=repo)
    out = repo.parent / "out.bundle"
    await git("bundle", "create", "-q", str(out), f"{spec.checkout}..{RESULT_REF}", cwd=repo)
    if out.stat().st_size > MAX_BUNDLE_BYTES:
        raise AgentError("the agent's commits are larger than the bundle limit")
    return head, base64.b64encode(out.read_bytes()).decode()


# ---------------------------------------------------------------- endpoints


@app.put("/runs/{run_id}/bundle", status_code=204)
async def upload_bundle(run_id: str, request: Request) -> Response:
    """Store the bundle the next ``POST /runs/<id>`` works on."""
    directory = run_dir(run_id)
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    size = 0
    with (directory / "in.bundle").open("wb") as fh:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BUNDLE_BYTES:
                raise HTTPException(status_code=413, detail="bundle too large")
            fh.write(chunk)
    return Response(status_code=204)


@app.post("/runs/{run_id}")
async def run(run_id: str, spec: RunSpec) -> RunResult:
    """Check out the uploaded bundle, run the agent and return its result."""
    directory = run_dir(run_id)
    bundle = directory / "in.bundle"
    if not bundle.is_file():
        raise HTTPException(status_code=404, detail="upload the bundle first")
    try:
        repo = directory / "repo"
        await checkout(bundle, repo, spec)
        result = await run_agent(spec, repo)
        out = RunResult(
            is_error=result.is_error,
            subtype=result.subtype,
            result=result.result or "",
            structured_output=result.structured_output,
            api_error_status=result.api_error_status,
        )
        if spec.commit_message and not result.is_error:
            commits = await collect_commits(repo, spec, spec.commit_message)
            if commits:
                out.head, out.bundle = commits
        return out
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


def main() -> None:
    """Serve on the unix socket, replacing one left over from a previous container."""
    socket_path = Path(os.getenv(RUNNER_SOCKET_ENV, DEFAULT_SOCKET))
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    uvicorn.run(app, uds=str(socket_path), log_level=os.getenv("RELAY_LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
