"""Git and GitHub operations done by the worker on the agent's behalf.

The credential only ever goes into the environment of a single git
subprocess or into a GitHub API request header; it is never written to disk,
put in command arguments, or shown to the agent.  The worker's clone is
private to the worker container: the agent gets a bundle of it and hands back
a bundle of its commits (see sandbox.py), and git never runs with the token in
a repository the agent has touched.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from common import (
    HTTP_CREATED,
    HTTP_OK,
    HTTP_UNPROCESSABLE,
    TRANSIENT,
    RejectedError,
    TransientError,
)
from sandbox import MAX_BUNDLE_BYTES, RESULT_REF

if TYPE_CHECKING:
    from pathlib import Path

    from projects import ProjectConfig

GIT_USERNAME = os.getenv("GIT_HTTP_USERNAME", "x-access-token")  # GitHub style
BOT_NAME = os.getenv("GIT_BOT_NAME", "relay-agent")
BOT_EMAIL = os.getenv("GIT_BOT_EMAIL", "relay-agent@users.noreply.github.com")
SCP_URL = re.compile(r"^git@([^:/]+):(.+)$")
MAX_PR_TITLE = 120
MAX_REVIEW_BODY = 60000


class GitError(RejectedError):
    """A git or GitHub step failed; the task fails with this message."""

    def __init__(self, message: str) -> None:
        """Prefix the message so the dashboard shows where it failed."""
        super().__init__(f"git_error: {message}")


@dataclass(frozen=True)
class RepoAccess:
    """A project together with the credential used to reach its repository."""

    project: ProjectConfig
    token: str

    @property
    def slug(self) -> str | None:
        """``org/app`` for GitHub-hosted repositories, otherwise None."""
        return github_slug(self.project.repo)


# ---------------------------------------------------------------- git


def clone_url(repo_url: str) -> str:
    """URL to clone with the token: ``git@host:org/app.git`` becomes HTTPS.

    The token is sent as an HTTP header, which SSH remotes would ignore.
    """
    match = SCP_URL.match(repo_url)
    return f"https://{match.group(1)}/{match.group(2)}" if match else repo_url


def auth_env(token: str) -> dict[str, str]:
    """Credentials for one git subprocess, via env-only config (nothing on disk)."""
    basic = base64.b64encode(f"{GIT_USERNAME}:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


async def git(*args: str, cwd: str | Path, token: str | None = None) -> str:
    """Run git and return stdout; raise ``GitError`` on failure."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(auth_env(token) if token else {})}
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        # Args never contain the token, so the message is safe to report.
        detail = err.decode(errors="replace").strip()[:500]
        raise GitError(f"git {args[0]} failed: {detail}")
    return out.decode().strip()


async def clone(access: RepoAccess, workdir: Path) -> None:
    """Clone the project into ``workdir`` and set the bot identity."""
    await git(
        "clone",
        "--no-tags",
        # No --filter: a partial clone would lazily fetch (without the token)
        # while bundling for the sandbox.
        clone_url(access.project.repo),
        str(workdir),
        cwd=workdir.parent,
        token=access.token,
    )
    await git("config", "user.name", BOT_NAME, cwd=workdir)
    await git("config", "user.email", BOT_EMAIL, cwd=workdir)


async def resolve(ref: str, workdir: Path) -> str:
    """Resolve a branch, tag or commit to a full commit SHA."""
    for candidate in (f"origin/{ref}", ref):
        try:
            return await git("rev-parse", "--verify", f"{candidate}^{{commit}}", cwd=workdir)
        except GitError:
            pass
    raise GitError(f"ref {ref!r} not found")


async def remote_branch(ref: str, fallback: str, workdir: Path) -> str:
    """``ref`` if it names a branch on origin, else ``fallback`` (for PR bases)."""
    try:
        await git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{ref}", cwd=workdir)
    except GitError:
        return fallback
    return ref


async def make_bundle(workdir: Path, refs: dict[str, str], dest: Path) -> Path:
    """Bundle ``refs/relay/<name>`` for each ``name -> sha`` (with full history)."""
    names = [f"refs/relay/{name}" for name in refs]
    for ref, sha in zip(names, refs.values(), strict=True):
        await git("update-ref", ref, sha, cwd=workdir)
    await git("bundle", "create", "-q", str(dest), *names, cwd=workdir)
    return dest


async def import_commits(workdir: Path, bundle_b64: str, base: str) -> str:
    """Fetch the sandbox's commits into the private clone; return their head SHA.

    The bundle is untrusted: objects are fsck-checked on fetch, and the result
    must descend from ``base`` so the agent cannot swap in unrelated history.
    """
    data = base64.b64decode(bundle_b64, validate=True)
    if len(data) > MAX_BUNDLE_BYTES:
        raise GitError("sandbox returned a bundle larger than the limit")
    path = workdir.parent / "result.bundle"
    path.write_bytes(data)
    await git("config", "fetch.fsckObjects", "true", cwd=workdir)
    await git("fetch", "-q", "--no-tags", str(path), RESULT_REF, cwd=workdir)
    sha = await git("rev-parse", "FETCH_HEAD", cwd=workdir)
    try:
        await git("merge-base", "--is-ancestor", base, sha, cwd=workdir)
    except GitError:
        raise GitError("sandbox commits do not build on the requested ref") from None
    if sha == base:
        raise GitError("sandbox returned no new commits")
    return sha


# ---------------------------------------------------------------- GitHub API


def github_slug(repo_url: str) -> str | None:
    """``git@github.com:org/app.git`` or ``https://github.com/org/app`` -> ``org/app``."""
    url = urlparse(clone_url(repo_url))
    if url.hostname == "github.com":
        return url.path.strip("/").removesuffix(".git") or None
    return None


async def github(
    method: str,
    path: str,
    token: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Call the GitHub REST API; raise ``TransientError`` on retryable failures."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        async with httpx.AsyncClient(base_url="https://api.github.com", timeout=30) as client:
            resp = await client.request(method, path, headers=headers, json=body, params=params)
    except httpx.HTTPError as exc:
        raise TransientError(f"github unreachable: {exc}") from exc
    if resp.status_code in TRANSIENT:
        raise TransientError(f"github returned {resp.status_code}")
    return resp


def pr_ref(pr: dict[str, Any]) -> dict[str, Any]:
    """The fields of a GitHub pull request that go into task output."""
    return {"pr": pr["number"], "pr_url": pr["html_url"]}


async def find_open_pr(slug: str, branch: str, token: str) -> dict[str, Any] | None:
    """The open pull request whose head is ``branch``, if any."""
    owner = slug.split("/", maxsplit=1)[0]
    found = await github(
        "GET",
        f"/repos/{slug}/pulls",
        token,
        params={"head": f"{owner}:{branch}", "state": "open"},
    )
    pulls = found.json() if found.status_code == HTTP_OK else []
    return pr_ref(pulls[0]) if pulls else None


async def open_pr(
    access: RepoAccess, branch: str, base: str, title: str, description: str
) -> dict[str, Any]:
    """Open (or, on redelivery, find) the pull request for ``branch``."""
    slug = access.slug
    if slug is None:
        return {"pr": None}  # not GitHub: branch is pushed, open the PR your own way
    resp = await github(
        "POST",
        f"/repos/{slug}/pulls",
        access.token,
        body={"title": title[:MAX_PR_TITLE], "head": branch, "base": base, "body": description},
    )
    if resp.status_code == HTTP_CREATED:
        return pr_ref(resp.json())
    if resp.status_code == HTTP_UNPROCESSABLE:  # most often: it already exists
        existing = await find_open_pr(slug, branch, access.token)
        if existing:
            return existing
    raise GitError(f"opening PR failed: GitHub returned {resp.status_code}")


def review_body(verdict: dict[str, Any]) -> str:
    """Markdown body for a review: summary followed by per-file comments."""
    lines = [str(verdict["summary"])]
    for comment in verdict.get("comments") or []:
        where = comment.get("file", "")
        if comment.get("line"):
            where += f":{comment['line']}"
        lines.append(f"- `{where}` {comment['comment']}")
    return "\n".join(lines)[:MAX_REVIEW_BODY]


async def post_review(access: RepoAccess, pr: int, sha: str, verdict: dict[str, Any]) -> bool:
    """Post the verdict as a GitHub review pinned to ``sha``; False if not GitHub."""
    slug = access.slug
    if slug is None:
        return False
    resp = await github(
        "POST",
        f"/repos/{slug}/pulls/{pr}/reviews",
        access.token,
        body={
            "commit_id": sha,
            "event": "APPROVE" if verdict["verdict"] == "approve" else "REQUEST_CHANGES",
            "body": review_body(verdict),
        },
    )
    if resp.status_code != HTTP_OK:
        # Most common cause: the reviewer uses the same token that opened the
        # PR, and GitHub does not let an account review its own pull request.
        raise GitError(f"posting review failed: GitHub returned {resp.status_code}")
    return True
