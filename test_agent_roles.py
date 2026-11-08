"""dev-team / reviewer / sandbox / model proxy / free-text orchestrator tests.

Uses a local bare git remote, the real sandbox app in-process with a fake agent
(``agent_runner.query``) and a fake LLM, so nothing reaches GitHub or the model API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import types
from pathlib import Path

import anthropic
import httpx
import pytest
from anthropic.types import ToolUseBlock
from claude_agent_sdk import CLIConnectionError, ResultMessage

import agent_roles
import agent_runner
import model_proxy
from common import TransientError
from handlers import make_orchestrator
from projects import parse_registry
from repo_ops import GitError, clone_url, github_slug
from sandbox import RESULT_REF, RunResult, Sandbox

TOKEN = "s3cr3t-token"
PROJECT = {
    "repo": "git@github.com:org/shop-api.git",
    "registry": "ghcr.io/org/shop-api",
    "deploy_target": "k8s/shop-api",
    "credentials": "secret/shop-api-bot",
}
BUILD = {"project": "shop-api", "ref": "main", "task": "Add a feature"}
REVIEW = {"project": "shop-api", "pr": 1}


def sh(*args: str, cwd: Path | str | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def result(**fields) -> ResultMessage:
    base = {"subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "is_error": False,
            "num_turns": 1, "session_id": "s"}
    return ResultMessage(**(base | fields))


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch, tmp_path):
    """Ignore the developer's git config (signing, hooks) and keep work dirs in tmp."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for module in (agent_roles, agent_runner):
        monkeypatch.setattr(module, "WORK_ROOT", str(tmp_path))


@pytest.fixture()
def remote(tmp_path) -> Path:
    """Bare repo with one commit on main containing AGENTS.md."""
    bare, seed = tmp_path / "remote.git", tmp_path / "seed"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    sh("git", "clone", "-q", str(bare), str(seed))
    (seed / "AGENTS.md").write_text("Use pytest.")
    sh("git", "add", ".", cwd=seed)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=seed)
    sh("git", "push", "-q", "origin", "main", cwd=seed)
    return bare


@pytest.fixture()
def projects(remote):
    """A valid registry entry pointed at the local remote (skipping URL validation)."""
    project = parse_registry({"projects": {"shop-api": PROJECT}})["shop-api"]
    return {"shop-api": project.model_copy(update={"repo": str(remote)})}


@pytest.fixture()
def secrets(tmp_path) -> str:
    path = tmp_path / "secrets" / "shop-api-bot"
    path.mkdir(parents=True)
    (path / "token").write_text(TOKEN + "\n")
    return str(path.parent)


@pytest.fixture()
def sandbox() -> Sandbox:
    """The real sandbox app, in-process instead of behind a unix socket."""
    return Sandbox(transport=httpx.ASGITransport(app=agent_runner.app))


def tree_contains(root: Path, needle: str) -> bool:
    return any(needle.encode() in p.read_bytes() for p in root.rglob("*") if p.is_file())


@pytest.fixture()
def agent(monkeypatch):
    """Replace the agent with ``behavior(cwd) -> ResultMessage`` (or an exception)."""
    seen: dict = {}

    def use(behavior):
        async def fake_query(*, prompt, options):
            seen.update(prompt=prompt, options=options)
            cwd = Path(options.cwd)
            assert not tree_contains(cwd, TOKEN), "token reachable by the agent"
            assert sh("git", "remote", cwd=cwd) == "", "sandbox clone has a remote"
            if isinstance(behavior, Exception):
                raise behavior
            yield behavior(options.cwd)

        monkeypatch.setattr(agent_runner, "query", fake_query)
        return seen

    return use


def call(handler, request, task_id: str = "t-1"):
    claim = {"task_id": task_id, "input": request if isinstance(request, str) else json.dumps(request)}

    async def run():
        async with httpx.AsyncClient() as client:
            return await handler(claim, client)

    return asyncio.run(run())


def commit_feature(cwd: str) -> ResultMessage:
    Path(cwd, "feature.py").write_text("print('hi')\n")
    sh("git", "add", ".", cwd=cwd)
    sh("git", "commit", "-qm", "add feature", cwd=cwd)
    return result(result="Added feature.py")


# ---------------------------------------------------------------- dev-team


@pytest.fixture()
def dev(projects, secrets, sandbox):
    return agent_roles.make_dev_team(projects, secrets, sandbox)


def test_dev_pushes_branch_and_reports_it(dev, agent, remote):
    seen = agent(commit_feature)
    status, out = call(dev, BUILD)
    assert status == "complete"
    output = json.loads(out)
    assert output["branch"] == "agent/t-1" and output["pr"] is None  # not GitHub
    assert sh("git", "rev-parse", "agent/t-1", cwd=remote) == output["sha"]
    assert TOKEN not in out
    options = seen["options"]
    assert "Use pytest." in options.system_prompt
    assert options.permission_mode == "dontAsk" and options.setting_sources == []
    assert seen["prompt"] == "Task:\nAdd a feature"


def test_dev_redelivery_reuses_branch(dev, agent):
    agent(commit_feature)
    assert call(dev, BUILD)[0] == "complete"
    assert call(dev, BUILD)[0] == "complete"


def test_dev_commits_leftovers(dev, agent, remote):
    def write_only(cwd):
        Path(cwd, "other.py").write_text("x = 1\n")
        return result(result="Wrote other.py")

    agent(write_only)
    status, out = call(dev, BUILD)
    assert status == "complete"
    assert sh("git", "log", "-1", "--format=%s", json.loads(out)["sha"], cwd=remote) == "agent: Add a feature"


@pytest.mark.parametrize(
    ("request_", "prefix"),
    [
        ({"project": "shop-api"}, "invalid_input"),
        ({"project": "nope", "task": "x"}, "unknown_project"),
        ({**BUILD, "ref": "a..b"}, "invalid_input"),
        ({**BUILD, "ref": "nosuch"}, "git_error"),
    ],
)
def test_dev_rejects_bad_requests(dev, agent, request_, prefix):
    agent(commit_feature)
    status, error = call(dev, request_)
    assert status == "fail" and error.startswith(prefix)


def test_dev_without_credential_fails(projects, tmp_path, sandbox):
    status, error = call(agent_roles.make_dev_team(projects, str(tmp_path), sandbox), BUILD)
    assert status == "fail" and error.startswith("credentials_unavailable")


def test_dev_rejects_commits_not_based_on_ref(projects, secrets, tmp_path, remote):
    """A compromised sandbox returning unrelated history must not get pushed."""
    other = tmp_path / "unrelated"
    sh("git", "init", "-q", str(other))
    (other / "evil.txt").write_text("x")
    sh("git", "add", ".", cwd=other)
    sh("git", "-c", "user.name=e", "-c", "user.email=e@e", "commit", "-qm", "evil", cwd=other)
    sh("git", "update-ref", RESULT_REF, "HEAD", cwd=other)
    sh("git", "bundle", "create", "-q", str(tmp_path / "evil.bundle"), RESULT_REF, cwd=other)

    class EvilSandbox:
        async def run(self, *_args):
            data = base64.b64encode((tmp_path / "evil.bundle").read_bytes()).decode()
            return RunResult(is_error=False, subtype="success", head="x", bundle=data)

    status, error = call(agent_roles.make_dev_team(projects, secrets, EvilSandbox()), BUILD)
    assert status == "fail" and "do not build on" in error
    assert "agent/t-1" not in sh("git", "branch", cwd=remote)


def test_unreachable_sandbox_is_transient(projects, secrets, tmp_path):
    dev = agent_roles.make_dev_team(projects, secrets, Sandbox(str(tmp_path / "no.sock")))
    with pytest.raises(TransientError):
        call(dev, BUILD)


@pytest.mark.parametrize(
    ("behavior", "prefix"),
    [
        (lambda cwd: result(result="Task unclear"), "no_changes"),
        (lambda cwd: result(is_error=True, subtype="error_max_turns", result="stuck"), "agent_error"),
    ],
)
def test_dev_agent_failures(dev, agent, behavior, prefix):
    agent(behavior)
    status, error = call(dev, BUILD)
    assert status == "fail" and error.startswith(prefix)


@pytest.mark.parametrize(
    "behavior",
    [lambda cwd: result(is_error=True, api_error_status=529), CLIConnectionError("down")],
)
def test_dev_transient_agent_failures_are_redelivered(dev, agent, behavior):
    agent(behavior)
    with pytest.raises(TransientError):
        call(dev, BUILD)


# ---------------------------------------------------------------- reviewer


@pytest.fixture()
def reviewer(projects, secrets, remote, dev, agent, sandbox):
    """Reviewer plus PR #1 on the remote, built by the dev-team handler."""
    agent(commit_feature)
    call(dev, BUILD)
    sh("git", "update-ref", "refs/pull/1/head", "refs/heads/agent/t-1", cwd=remote)
    return agent_roles.make_reviewer(projects, secrets, sandbox)


VERDICT = {"verdict": "request_changes", "summary": "Needs a test",
           "comments": [{"file": "feature.py", "line": 1, "comment": "add test"}]}


def test_review_reports_verdict_with_read_only_tools(reviewer, agent):
    reviewed = {}

    def verdict(cwd):
        reviewed["head"] = sh("git", "rev-parse", "HEAD", cwd=cwd)
        return result(result="", structured_output=VERDICT)

    seen = agent(verdict)
    status, out = call(reviewer, REVIEW, "r-1")
    assert status == "complete"
    output = json.loads(out)
    assert output["verdict"] == "request_changes" and output["posted"] is False
    options = seen["options"]
    assert not {"Edit", "Write"} & set(options.allowed_tools)
    assert options.output_format == agent_roles.VERDICT_SCHEMA
    assert reviewed["head"] == output["sha"]  # the sandbox checked out the PR head


def test_review_keeps_verdict_when_posting_fails(reviewer, agent, monkeypatch):
    async def refuse(*_args):
        raise GitError("posting review failed: GitHub returned 422")

    monkeypatch.setattr(agent_roles, "post_review", refuse)
    agent(lambda cwd: result(result="", structured_output=VERDICT))
    output = json.loads(call(reviewer, REVIEW, "r-2")[1])
    assert output["verdict"] == "request_changes"
    assert output["posted"] is False and "422" in output["post_error"]


@pytest.mark.parametrize(
    ("request_", "prefix"),
    [
        (REVIEW, "agent_error"),  # agent returned no structured verdict
        ({"project": "shop-api"}, "invalid_input"),
        ({"project": "shop-api", "pr": 99}, "git_error"),
    ],
)
def test_review_failures(reviewer, agent, request_, prefix):
    agent(lambda cwd: result(result="oops"))
    status, error = call(reviewer, request_, "r-3")
    assert status == "fail" and error.startswith(prefix)


# ---------------------------------------------------------------- guard hook


@pytest.mark.parametrize(
    ("tool", "args", "denied"),
    [
        ("Read", {"file_path": "/etc/passwd"}, True),
        ("Edit", {"file_path": "../../x"}, True),
        ("Read", {"file_path": "src/app.py"}, False),
        ("Grep", {"pattern": "x"}, False),
        ("Bash", {"command": "git push -f origin HEAD"}, True),
        ("Bash", {"command": "git remote set-url origin x"}, True),
        ("Bash", {"command": "pytest -q"}, False),
    ],
)
def test_guard(tmp_path, tool, args, denied):
    hook = agent_runner.guard(tmp_path)[0].hooks[0]
    out = asyncio.run(hook({"tool_name": tool, "tool_input": args}, None, None))
    decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
    assert (decision == "deny") is denied


# ---------------------------------------------------------------- repo_ops


@pytest.mark.parametrize(
    ("url", "https", "slug"),
    [
        ("git@github.com:org/app.git", "https://github.com/org/app.git", "org/app"),
        ("https://github.com/org/app", "https://github.com/org/app", "org/app"),
        ("git@gitlab.com:org/app.git", "https://gitlab.com/org/app.git", None),
        ("/srv/git/app.git", "/srv/git/app.git", None),
    ],
)
def test_clone_url_and_slug(url, https, slug):
    assert clone_url(url) == https
    assert github_slug(url) == slug


# ---------------------------------------------------------------- model proxy


def test_model_proxy_swaps_in_key_and_streams():
    seen = {}

    def upstream(req: httpx.Request) -> httpx.Response:
        seen.update(url=str(req.url), headers=req.headers, body=req.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: hi\n\n")

    proxy = model_proxy.create_app("real-key", httpx.MockTransport(upstream))

    async def go():
        transport = httpx.ASGITransport(app=proxy)
        async with httpx.AsyncClient(transport=transport, base_url="http://p") as client:
            ok = await client.post(
                "/v1/messages?beta=true",
                headers={"x-api-key": "placeholder", "authorization": "Bearer x",
                         "anthropic-version": "2023-06-01"},
                content=b"{}",
            )
            other = await client.get("/api/oauth/token")
            return ok, other

    ok, other = asyncio.run(go())
    assert ok.status_code == 200 and ok.content == b"data: hi\n\n"
    assert seen["url"] == "https://api.anthropic.com/v1/messages?beta=true"
    assert seen["headers"]["x-api-key"] == "real-key" and "authorization" not in seen["headers"]
    assert seen["headers"]["anthropic-version"] == "2023-06-01" and seen["body"] == b"{}"
    assert other.status_code == 404


# ---------------------------------------------------------------- free-text orchestrator


class FakeLLM:
    """Stands in for ``AsyncAnthropic``: returns one route_task call or raises."""

    def __init__(self, out=None, exc=None):
        self.out, self.exc, self.messages = out, exc, self

    async def create(self, **kwargs):
        assert kwargs["tool_choice"]["name"] == "route_task"
        if self.exc:
            raise self.exc
        block = ToolUseBlock(type="tool_use", id="t", name="route_task", input=self.out)
        return types.SimpleNamespace(content=[block])


def api_error(status: int) -> anthropic.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "http://x"))
    return anthropic.APIStatusError("e", response=response, body=None)


@pytest.fixture()
def orchestrate():
    """Run an orchestrator with ``llm`` against a fake relay; return (outcome, child input)."""
    registry = parse_registry({"projects": {"shop-api": PROJECT}})
    roles = {"dev-team": "dev", "reviewer": "rev", "deployer": "dep"}

    def run(task_input, llm=None):
        posted = {}

        def relay(req):
            posted.update(json.loads(json.loads(req.content)["input"]))
            return httpx.Response(201, json={"task_id": "child"})

        async def go():
            transport = httpx.MockTransport(relay)
            async with httpx.AsyncClient(transport=transport, base_url="http://r") as client:
                claim = {"task_id": "o-1", "input": task_input}
                return await make_orchestrator(registry, roles, llm)(claim, client)

        return asyncio.run(go()), posted

    return run


def test_orchestrator_forwards_review_pr(orchestrate):
    (status, _), child = orchestrate(json.dumps({"project": "shop-api", "action": "review", "pr": 3}))
    assert status == "complete" and child["pr"] == 3


def test_orchestrator_build_needs_task(orchestrate):
    (status, error), _ = orchestrate(json.dumps({"project": "shop-api", "action": "build"}))
    assert status == "fail" and error.startswith("invalid_input")


def test_orchestrator_routes_free_text(orchestrate):
    llm = FakeLLM({"project": "shop-api", "action": "build", "task": "Add login"})
    (status, out), child = orchestrate("add login to shop-api", llm)
    assert status == "complete" and json.loads(out)["interpreted_from_text"]
    assert child["task"] == "Add login" and child["action"] == "build"


def test_orchestrator_json_skips_llm(orchestrate):
    llm = FakeLLM(exc=AssertionError("LLM must not be called for JSON"))
    (status, out), _ = orchestrate(json.dumps({"project": "shop-api"}), llm)
    assert status == "complete" and not json.loads(out)["interpreted_from_text"]


@pytest.mark.parametrize(
    ("llm", "prefix"),
    [
        (FakeLLM({"clarify": "Which project?"}), "needs_clarification"),
        (FakeLLM({"action": "deploy"}), "needs_clarification"),
        (FakeLLM(exc=api_error(400)), "router_error"),
    ],
)
def test_orchestrator_free_text_failures(orchestrate, llm, prefix):
    (status, error), _ = orchestrate("ship it", llm)
    assert status == "fail" and error.startswith(prefix)


def test_orchestrator_overloaded_llm_is_transient(orchestrate):
    with pytest.raises(TransientError):
        orchestrate("ship it", FakeLLM(exc=api_error(529)))
