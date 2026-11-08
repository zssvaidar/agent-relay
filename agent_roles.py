"""Agent handlers: ``dev-team`` (writes code) and ``reviewer`` (reviews PRs).

This is the trusted half, run by the worker. The Claude agent itself runs in
the pod's sandbox container (agent_runner.py), which holds no credentials.

Per task the worker:

1. validates the request against the registry and reads the project token;
2. clones the repo into a directory private to the worker container;
3. sends the sandbox a git bundle plus instructions (sandbox.py) and waits;
4. imports and checks any commits the agent made (repo_ops.import_commits),
   then pushes, opens the PR or posts the review with the token.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from common import (
    AgentError,
    Handler,
    Outcome,
    RejectedError,
    load_request,
    lookup_project,
    rejecting,
    require_credential,
    require_pr,
    require_ref,
    require_task,
)
from repo_ops import (
    GitError,
    RepoAccess,
    clone,
    git,
    import_commits,
    make_bundle,
    open_pr,
    post_review,
    remote_branch,
    resolve,
)
from sandbox import RunResult, RunSpec, Sandbox

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx

    from projects import ProjectConfig

WORK_ROOT = os.getenv("RELAY_WORK_ROOT", tempfile.gettempdir())
MAX_SUMMARY = 2000
SHA = re.compile(r"[0-9a-f]{7,40}")
DEV_TOOLS = ["Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Bash"]
REVIEW_TOOLS = ["Read", "Glob", "Grep", "Bash"]  # no Edit/Write


@dataclass(frozen=True)
class TaskContext:
    """A validated task: its request, the project's repo access and the ref."""

    key: str
    request: dict[str, Any]
    access: RepoAccess
    ref: str


def load_task(
    claim: dict[str, Any], projects: dict[str, ProjectConfig], secrets_dir: str | None
) -> TaskContext:
    """Validate the claim against the registry or raise ``RejectedError``."""
    request = load_request(claim["input"])
    key = request["project"]
    project = lookup_project(projects, key)
    ref = require_ref(request, project)
    token = require_credential(project, secrets_dir)
    return TaskContext(key, request, RepoAccess(project, token), ref)


def safe_id(claim: dict[str, Any]) -> str:
    """The task id reduced to characters safe in paths, branches and run ids."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(claim["task_id"]))[:80]


@contextlib.contextmanager
def task_workspace(claim: dict[str, Any]) -> Iterator[Path]:
    """Yield a clone path in a fresh private directory that is removed afterwards."""
    parent = Path(tempfile.mkdtemp(prefix=f"task-{safe_id(claim)}-", dir=WORK_ROOT))
    try:
        yield parent / "repo"
    finally:
        shutil.rmtree(parent, ignore_errors=True)


async def run_in_sandbox(
    sandbox: Sandbox, claim: dict[str, Any], workdir: Path, refs: dict[str, str], spec: RunSpec
) -> RunResult:
    """Bundle ``refs`` from the private clone and run the agent on them."""
    bundle = await make_bundle(workdir, refs, workdir.parent / "task.bundle")
    run_id = f"{safe_id(claim)}-{uuid.uuid4().hex[:8]}"
    return await sandbox.run(run_id, bundle, spec)


def summary(result: RunResult) -> str:
    """Return the agent's final text, truncated for the dashboard."""
    return result.result[:MAX_SUMMARY]


# ---------------------------------------------------------------- dev-team

DEV_SYSTEM = """You are the developer on an automated software team. You work in a
fresh clone of the repository (your current directory) on a branch made for this
task. Implement the task, follow the repository's conventions, run its tests and
linters, and commit your work with clear messages. There is no git remote: the
worker pushes and opens the pull request after you finish.
If the task is unclear or impossible, make no commits and explain why.
End with a short summary of what you changed and how you tested it."""


def branch_name(claim: dict[str, Any]) -> str:
    """Deterministic per-task branch, so a redelivered task reuses its branch and PR."""
    return f"agent/{safe_id(claim)}"


def make_dev_team(
    projects: dict[str, ProjectConfig],
    secrets_dir: str | None = None,
    sandbox: Sandbox | None = None,
) -> Handler:
    """Build the handler that implements a task with the agent and opens a PR."""
    sandbox = sandbox or Sandbox()

    @rejecting
    async def build(claim: dict[str, Any], _client: httpx.AsyncClient) -> Outcome:
        ctx = load_task(claim, projects, secrets_dir)
        task = require_task(ctx.request)
        branch = branch_name(claim)

        with task_workspace(claim) as workdir:
            await clone(ctx.access, workdir)
            base = await resolve(ctx.ref, workdir)
            # A PR needs a branch as its base; for a tag or commit use the default.
            pr_base = await remote_branch(ctx.ref, ctx.access.project.default_branch, workdir)
            spec = RunSpec(
                system=DEV_SYSTEM,
                prompt=f"Task:\n{task}",
                tools=DEV_TOOLS,
                checkout=base,
                branch=branch,
                commit_message=f"agent: {task.splitlines()[0][:72]}",
            )
            result = await run_in_sandbox(sandbox, claim, workdir, {"base": base}, spec)
            if result.is_error:
                raise AgentError(f"{result.subtype}: {summary(result)}")
            if not result.bundle:
                raise RejectedError(f"no_changes: {summary(result)}")
            sha = await import_commits(workdir, result.bundle, base)
            await git(
                "push",
                "--force",
                "origin",
                f"{sha}:refs/heads/{branch}",
                cwd=workdir,
                token=ctx.access.token,
            )

        pr = await open_pr(ctx.access, branch, pr_base, task.splitlines()[0], summary(result))
        output = {"project": ctx.key, "branch": branch, "base": base, "sha": sha}
        return "complete", json.dumps({**output, **pr, "summary": summary(result)}, sort_keys=True)

    return build


# ---------------------------------------------------------------- reviewer

REVIEW_SYSTEM = """You are the code reviewer on an automated software team. Your current
directory is a clone checked out at the pull request's head commit. Review the
change for correctness, security, tests and fit with the repository's
conventions. You may run tests and linters, but do not edit files. Approve only
if you would merge it as is; otherwise request changes with specific, actionable
comments."""

VERDICTS = ("approve", "request_changes")
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "summary": {"type": "string"},
            "comments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "comment": {"type": "string"},
                    },
                    "required": ["comment"],
                },
            },
        },
        "required": ["verdict", "summary"],
    },
}


async def pr_commits(ctx: TaskContext, pr: int, workdir: Path) -> tuple[str, str]:
    """Fetch the PR and return ``(sha to review, merge base)``."""
    await git("fetch", "origin", f"pull/{pr}/head", cwd=workdir, token=ctx.access.token)
    head = await git("rev-parse", "FETCH_HEAD", cwd=workdir)
    # If the orchestrator pinned a commit, review exactly that one.
    sha = await resolve(ctx.ref, workdir) if SHA.fullmatch(ctx.ref) else head
    base = await git("merge-base", f"origin/{ctx.access.project.default_branch}", sha, cwd=workdir)
    return sha, base


def parse_verdict(result: RunResult) -> dict[str, Any]:
    """Return the agent's structured verdict or raise ``AgentError``."""
    verdict = None if result.is_error else result.structured_output
    if not isinstance(verdict, dict) or verdict.get("verdict") not in VERDICTS:
        raise AgentError(f"no valid verdict ({result.subtype}): {summary(result)}")
    return verdict


def make_reviewer(
    projects: dict[str, ProjectConfig],
    secrets_dir: str | None = None,
    sandbox: Sandbox | None = None,
) -> Handler:
    """Build the handler that reviews a PR with the agent and posts the verdict."""
    sandbox = sandbox or Sandbox()

    @rejecting
    async def review(claim: dict[str, Any], _client: httpx.AsyncClient) -> Outcome:
        ctx = load_task(claim, projects, secrets_dir)
        pr = require_pr(ctx.request)

        with task_workspace(claim) as workdir:
            await clone(ctx.access, workdir)
            sha, base = await pr_commits(ctx, pr, workdir)
            spec = RunSpec(
                system=REVIEW_SYSTEM,
                prompt=f"Review pull request #{pr}. The change is `git diff {base}...HEAD`.",
                tools=REVIEW_TOOLS,
                checkout=sha,
                output_format=VERDICT_SCHEMA,
            )
            refs = {"head": sha, "base": base}
            verdict = parse_verdict(await run_in_sandbox(sandbox, claim, workdir, refs, spec))

        output = {
            "project": ctx.key,
            "pr": pr,
            "sha": sha,
            "verdict": verdict["verdict"],
            "summary": str(verdict["summary"])[:MAX_SUMMARY],
        }
        # Keep the verdict even if GitHub refuses it, rather than re-running the review.
        try:
            output["posted"] = await post_review(ctx.access, pr, sha, verdict)
        except GitError as exc:
            output.update(posted=False, post_error=str(exc))
        return "complete", json.dumps(output, sort_keys=True)

    return review


__all__ = ["make_dev_team", "make_reviewer"]
