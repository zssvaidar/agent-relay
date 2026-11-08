"""dev-team / reviewer / sandbox / model proxy / free-text orchestrator tests.

Uses a local bare git remote, the real sandbox app in-process with a fake
``claude -p`` (``claude_cli.run_claude``) and a fake router runner, so nothing
reaches GitHub or the model. The claude_cli tests run a stub ``claude`` script.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import agent_guard
import agent_roles
import agent_runner
import claude_cli
import model_proxy
from claude_cli import ClaudeResult
from common import AgentError, TransientError
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


def result(**fields) -> ClaudeResult:
    base = {"subtype": "success", "is_error": False, "result": ""}
    return ClaudeResult(**(base | fields))


def flag(args: list[str], name: str) -> str:
    """Value following ``name`` in a claude argv."""
    return args[args.index(name) + 1]


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
    """Replace ``claude -p`` with ``behavior(cwd) -> ClaudeResult`` (or an exception)."""
    seen: dict = {}

    def use(behavior):
        async def fake_run(prompt, args, *, cwd, timeout, env=None):
            seen.update(prompt=prompt, args=list(args), cwd=cwd, env=env)
            assert not tree_contains(Path(cwd), TOKEN), "token reachable by the agent"
            assert sh("git", "remote", cwd=cwd) == "", "sandbox clone has a remote"
            if isinstance(behavior, Exception):
                raise behavior
            return behavior(cwd)

        monkeypatch.setattr(claude_cli, "run_claude", fake_run)
        return seen

    return use


def call(handler, request, task_id: str = "t-1"):
    claim = {"task_id": task_id, "input": request if isinstance(request, str) else json.dumps(request)}

    async def run():
        async with httpx.AsyncClient() as client:
            return await handler(claim, client)

    return asyncio.run(run())


def commit_feature(cwd: str) -> ClaudeResult:
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
    args = seen["args"]
    assert "Use pytest." in flag(args, "--system-prompt")
    assert flag(args, "--permission-mode") == "dontAsk" and flag(args, "--setting-sources") == ""
    assert "agent_guard.py" in flag(args, "--settings")
    assert seen["env"] == {agent_guard.ROOT_ENV: seen["cwd"]}
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


def test_dev_transient_agent_failures_are_redelivered(dev, agent):
    # run_claude raises TransientError for a missing CLI or a 429/5xx (test_claude_cli_errors).
    agent(TransientError("model API returned 529"))
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
    args = seen["args"]
    assert not {"Edit", "Write"} & set(flag(args, "--allowedTools").split(","))
    assert flag(args, "--tools") == flag(args, "--allowedTools")
    assert json.loads(flag(args, "--json-schema")) == agent_roles.VERDICT_SCHEMA["schema"]
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
    out = agent_guard.decide({"tool_name": tool, "tool_input": args}, tmp_path)
    decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
    assert (decision == "deny") is denied


def run_guard(stdin: str, root: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != agent_guard.ROOT_ENV}
    if root:
        env[agent_guard.ROOT_ENV] = root
    out = subprocess.run([sys.executable, agent_guard.__file__], input=stdin, env=env,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_guard_script_allows_inside_and_fails_closed(tmp_path):
    call_ = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "a.py"}})
    assert run_guard(call_, str(tmp_path)) == {}
    for stdin, root in ((call_, None), ("not json", str(tmp_path))):
        assert run_guard(stdin, root)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------- claude_cli


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    """Point claude_cli at a stub script that runs ``body`` (Python) and records its argv."""

    def use(body: str) -> Path:
        script = tmp_path / "claude"
        script.write_text(f"#!{sys.executable}\nimport json, sys, time\n"
                          f"open({str(tmp_path / 'argv')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                          f"prompt = sys.stdin.read()\n{body}\n")
        script.chmod(0o755)
        monkeypatch.setattr(claude_cli, "CLAUDE_BIN", str(script))
        return tmp_path / "argv"

    return use


def run_cli(tmp_path, timeout=30):
    return asyncio.run(claude_cli.run_claude("hello", ["--model", "m"], cwd=str(tmp_path), timeout=timeout))


def test_claude_cli_parses_result(fake_claude, tmp_path):
    argv = fake_claude('print("noise"); print(json.dumps({"type": "result", "subtype": "success", '
                       '"is_error": False, "result": prompt, "structured_output": {"a": 1}}))')
    out = run_cli(tmp_path)
    assert out.result == "hello" and out.structured_output == {"a": 1} and not out.is_error
    assert json.loads(argv.read_text())[:3] == ["-p", "--output-format", "json"]


def test_claude_cli_failed_run_still_returns_result(fake_claude, tmp_path):
    fake_claude('print(json.dumps({"type": "result", "subtype": "error_max_turns", '
                '"is_error": True})); sys.exit(1)')
    out = run_cli(tmp_path)
    assert out.is_error and out.subtype == "error_max_turns"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('print(json.dumps({"type": "result", "is_error": True, "api_error_status": 529}))',
         TransientError),
        ('sys.stderr.write("boom"); sys.exit(2)', AgentError),
        ("time.sleep(10)", AgentError),
    ],
)
def test_claude_cli_errors(fake_claude, tmp_path, body, error):
    fake_claude(body)
    with pytest.raises(error):
        run_cli(tmp_path, timeout=1)


def test_claude_cli_missing_binary_is_transient(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_cli, "CLAUDE_BIN", str(tmp_path / "nope"))
    with pytest.raises(TransientError):
        run_cli(tmp_path)


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

    proxy = model_proxy.create_app("real-key", httpx.MockTransport(upstream), oauth_token="")

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


def test_model_proxy_prefers_subscription_token():
    seen = {}

    def upstream(req: httpx.Request) -> httpx.Response:
        seen.update(headers=req.headers)
        return httpx.Response(200, content=b"{}")

    proxy = model_proxy.create_app("real-key", httpx.MockTransport(upstream), oauth_token="oat-1")

    async def go():
        transport = httpx.ASGITransport(app=proxy)
        async with httpx.AsyncClient(transport=transport, base_url="http://p") as client:
            await client.post("/v1/messages", headers={"x-api-key": "placeholder",
                                                       "anthropic-beta": "a-1"}, content=b"{}")

    asyncio.run(go())
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer oat-1" and "x-api-key" not in headers
    assert headers["anthropic-beta"] == f"a-1,{model_proxy.OAUTH_BETA}"


# ---------------------------------------------------------------- free-text orchestrator


class FakeLLM:
    """Stands in for the router's ``claude -p`` runner: returns structured output or raises."""

    def __init__(self, out=None, exc=None, **fields):
        self.out, self.exc, self.fields = out, exc, fields

    async def __call__(self, prompt, args):
        assert flag(args, "--tools") == "" and "--json-schema" in args
        assert prompt.startswith("<request>")
        if self.exc:
            raise self.exc
        return result(structured_output=self.out, **self.fields)


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


@pytest.mark.parametrize(("author", "ok"), [("zssvaidar", True), ("someone-else", False), ("", False)])
def test_orchestrator_only_accepts_issues_from_owner(orchestrate, author, ok):
    request = {"project": "shop-api", "action": "build", "task": "Add login", "issue_author": author}
    (status, error), child = orchestrate(json.dumps(request))
    if ok:
        assert status == "complete" and child["task"] == "Add login"
    else:
        assert status == "fail" and error.startswith("forbidden_author") and not child


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
        (FakeLLM(is_error=True, subtype="error_during_execution"), "router_error"),
        (FakeLLM(exc=AgentError("timed out after 120s")), "router_error"),
    ],
)
def test_orchestrator_free_text_failures(orchestrate, llm, prefix):
    (status, error), _ = orchestrate("ship it", llm)
    assert status == "fail" and error.startswith(prefix)


def test_orchestrator_overloaded_llm_is_transient(orchestrate):
    with pytest.raises(TransientError):
        orchestrate("ship it", FakeLLM(exc=TransientError("model API returned 529")))
