"""Project registry and orchestrator/deployer handler tests."""

from __future__ import annotations

import os

os.environ.setdefault("RELAY_DATABASE_URL", "sqlite:////tmp/agent-relay-test.db")

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from database import Base, engine
from handlers import make_deployer, make_orchestrator
from projects import RegistryError, load_registry, parse_registry

REPO_ROOT = Path(__file__).parent
SECRET_VALUE = "ghp_should_never_leak_123"


def registry(**overrides) -> dict:
    project = {
        "repo": "git@github.com:zssvaidar/cp-expiry-tracker.git",
        "default_branch": "main",
        "registry": "ghcr.io/zssvaidar/cp-expiry-tracker",
        "deploy_target": "k8s/cp-expiry-tracker",
        "credentials": "secret/cp-expiry-tracker-bot",
    }
    project.update(overrides)
    return {"projects": {"cp-expiry-tracker": project}}


# --- registry ---------------------------------------------------------------

def test_registry_parses_and_exposes_only_public_fields():
    projects = parse_registry(registry())
    project = projects["cp-expiry-tracker"]
    assert project.deploy_target.kind == "k8s"
    assert project.deploy_target.name == "cp-expiry-tracker"
    assert project.secret_name == "cp-expiry-tracker-bot"
    assert "credentials" not in project.public_view("cp-expiry-tracker")


@pytest.mark.parametrize(
    "override",
    [
        {"credentials": "ghp_rawtoken"},
        {"credentials": "secret/Bad_Name"},
        {"deploy_target": "heroku/app"},
        {"deploy_target": "k8s"},
        {"repo": "/local/path"},
        {"unexpected": "field"},
    ],
)
def test_registry_rejects_invalid_projects(override):
    with pytest.raises(RegistryError):
        parse_registry(registry(**override))


def test_registry_rejects_bad_keys_and_shape():
    with pytest.raises(RegistryError):
        parse_registry({"projects": {"Bad Key": registry()["projects"]["cp-expiry-tracker"]}})
    with pytest.raises(RegistryError):
        parse_registry({"nope": {}})


def test_registry_error_does_not_echo_raw_credential():
    with pytest.raises(RegistryError) as info:
        parse_registry(registry(credentials=SECRET_VALUE))
    assert SECRET_VALUE not in str(info.value)


def test_chart_values_registry_is_valid(tmp_path):
    """The projects block in values.yaml must pass the same validation."""

    import yaml

    values = yaml.safe_load((REPO_ROOT / "chart/agent-relay/values.yaml").read_text())
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump({"projects": values["projects"]}))
    assert "cp-expiry-tracker" in load_registry(path)


# --- handlers against a real relay ------------------------------------------

@pytest.fixture()
def relay():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(main.app) as client:
        yield client
    Base.metadata.drop_all(engine)


def register(client: TestClient, name: str) -> dict:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201
    return response.json()


def run_one(relay: TestClient, sender: dict, recipient: dict, handler, task_input: str) -> dict:
    """Send one task, run ``handler`` on it as the recipient, return the task."""

    sent = relay.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {sender['token']}"},
        json={"to": recipient["agent_id"], "input": task_input},
    ).json()
    headers = {"Authorization": f"Bearer {recipient['token']}"}
    claim = relay.post("/api/v1/tasks/claim", headers=headers, json={"wait_seconds": 0}).json()

    async def handle():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://relay", headers=headers) as client:
            return await handler(claim, client)

    action, value = asyncio.run(handle())
    field = "output" if action == "complete" else "error"
    relay.post(
        f"/api/v1/tasks/{sent['task_id']}/{action}",
        headers=headers,
        json={"claim_token": claim["claim_token"], field: value},
    )
    return relay.get(f"/api/v1/tasks/{sent['task_id']}", headers=headers).json() | {"claim": claim}


def test_orchestrator_dispatches_to_deployer_without_credentials(relay):
    sender, orch, deployer = (register(relay, n) for n in ("ops", "orchestrator", "deployer"))
    handler = make_orchestrator(parse_registry(registry()), {"deployer": deployer["agent_id"]})

    task = run_one(relay, sender, orch, handler, json.dumps({"project": "cp-expiry-tracker", "action": "deploy"}))

    assert task["status"] == "completed"
    output = json.loads(task["output"])
    assert output["dispatched_to"] == "deployer"
    child = relay.get(
        f"/api/v1/tasks/{output['child_task_id']}", headers={"Authorization": f"Bearer {deployer['token']}"}
    ).json()
    child_input = json.loads(child["input"])
    assert child_input["deploy_target"] == "k8s/cp-expiry-tracker"
    assert child_input["ref"] == "main"
    assert "credentials" not in child_input and "cp-expiry-tracker-bot" not in child["input"]


def test_orchestrator_redelivery_does_not_duplicate_child(relay):
    sender, orch, deployer = (register(relay, n) for n in ("ops", "orchestrator", "deployer"))
    handler = make_orchestrator(parse_registry(registry()), {"deployer": deployer["agent_id"]})
    sent = relay.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {sender['token']}"},
        json={"to": orch["agent_id"], "input": json.dumps({"project": "cp-expiry-tracker"})},
    ).json()
    claim = {"task_id": sent["task_id"], "input": json.dumps({"project": "cp-expiry-tracker"})}

    async def twice():
        transport = httpx.ASGITransport(app=main.app)
        headers = {"Authorization": f"Bearer {orch['token']}"}
        async with httpx.AsyncClient(transport=transport, base_url="http://relay", headers=headers) as client:
            return [await handler(claim, client) for _ in range(2)]

    first, second = asyncio.run(twice())
    assert first == second
    inbox = relay.get(
        "/api/v1/tasks", params={"direction": "received"}, headers={"Authorization": f"Bearer {deployer['token']}"}
    ).json()
    assert len(inbox["items"]) == 1


@pytest.mark.parametrize(
    ("task_input", "error_prefix"),
    [
        ("deploy please", "invalid_input"),
        (json.dumps({"project": "nope"}), "unknown_project"),
        (json.dumps({"project": "cp-expiry-tracker", "action": "rm-rf"}), "invalid_input"),
        (json.dumps({"project": "cp-expiry-tracker", "ref": "../../etc"}), "invalid_input"),
        (json.dumps({"project": "cp-expiry-tracker", "action": "review", "pr": 1}), "no_agent"),
    ],
)
def test_orchestrator_fails_bad_requests(relay, task_input, error_prefix):
    sender, orch, deployer = (register(relay, n) for n in ("ops", "orchestrator", "deployer"))
    handler = make_orchestrator(parse_registry(registry()), {"deployer": deployer["agent_id"]})
    task = run_one(relay, sender, orch, handler, task_input)
    assert task["status"] == "failed"
    assert task["error"].startswith(error_prefix)


def test_deployer_uses_mounted_secret_and_never_echoes_it(relay, tmp_path):
    sender, deployer = register(relay, "orchestrator"), register(relay, "deployer")
    projects = parse_registry(registry())
    request = json.dumps({"project": "cp-expiry-tracker", "ref": "v1.0.0"})

    missing = run_one(relay, sender, deployer, make_deployer(projects, str(tmp_path)), request)
    assert missing["status"] == "failed"
    assert missing["error"].startswith("credentials_unavailable")

    (tmp_path / "cp-expiry-tracker-bot").mkdir()
    (tmp_path / "cp-expiry-tracker-bot" / "token").write_text(SECRET_VALUE + "\n")
    done = run_one(relay, sender, deployer, make_deployer(projects, str(tmp_path)), request)
    assert done["status"] == "completed"
    assert json.loads(done["output"])["image"] == "ghcr.io/zssvaidar/cp-expiry-tracker:v1.0.0"
    assert SECRET_VALUE not in done["output"]
