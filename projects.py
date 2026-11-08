"""Project registry owned by the orchestrator.

The registry is a YAML file (rendered by the Helm chart from ``values.yaml``
into ConfigMap ``relay-projects``) that lists every project the agents may
work on.  It holds only *references* to credentials, never their values:
``credentials: secret/<name>`` names a Kubernetes Secret that is mounted, read
only, into the workers that actually need it (the deployer).

Task input must only ever carry a project key; workers resolve the rest here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


PROJECTS_FILE_ENV = "RELAY_PROJECTS_FILE"
SECRETS_DIR_ENV = "RELAY_PROJECT_SECRETS_DIR"
DEFAULT_SECRETS_DIR = "/var/run/secrets/projects"

PROJECT_KEY = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Kubernetes Secret names are DNS-1123 subdomains; keep to the simple form.
SECRET_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,251}[a-z0-9])?$")
DEPLOY_KINDS = ("k8s", "vercel")


class RegistryError(ValueError):
    """The registry file is missing or invalid."""


class DeployTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["k8s", "vercel"]
    name: str


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str = Field(min_length=1)
    default_branch: str = Field(default="main", min_length=1)
    registry: str = Field(min_length=1)
    deploy_target: DeployTarget
    credentials: str

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if not (value.startswith("git@") or value.startswith("https://")):
            raise ValueError("must be a git@ or https:// URL")
        return value

    @field_validator("deploy_target", mode="before")
    @classmethod
    def parse_deploy_target(cls, value: object) -> object:
        if isinstance(value, str):
            kind, sep, name = value.partition("/")
            if not sep or not name or kind not in DEPLOY_KINDS:
                raise ValueError(f"must look like <kind>/<name> with kind in {DEPLOY_KINDS}")
            return {"kind": kind, "name": name}
        return value

    @field_validator("credentials")
    @classmethod
    def validate_credentials(cls, value: str) -> str:
        prefix, sep, name = value.partition("/")
        if prefix != "secret" or not sep or not SECRET_NAME.match(name):
            raise ValueError("must be a reference of the form secret/<k8s-secret-name>, never a raw value")
        return value

    @property
    def secret_name(self) -> str:
        return self.credentials.partition("/")[2]

    def public_view(self, key: str) -> dict[str, str]:
        """Fields safe to put in task input (stored in the DB, shown on the dashboard)."""

        return {
            "project": key,
            "repo": self.repo,
            "default_branch": self.default_branch,
            "registry": self.registry,
            "deploy_target": f"{self.deploy_target.kind}/{self.deploy_target.name}",
        }


def parse_registry(data: object) -> dict[str, ProjectConfig]:
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        raise RegistryError("registry must be a mapping with a top-level 'projects' mapping")
    projects: dict[str, ProjectConfig] = {}
    for key, raw in data["projects"].items():
        if not isinstance(key, str) or not PROJECT_KEY.match(key):
            raise RegistryError(f"project key {key!r} must be a lowercase slug")
        try:
            projects[key] = ProjectConfig.model_validate(raw)
        except ValidationError as exc:
            # Pydantic messages can echo input values; the registry holds no
            # secrets by design, but keep messages to field names anyway.
            fields = ", ".join(".".join(str(p) for p in err["loc"]) or "<root>" for err in exc.errors())
            raise RegistryError(f"project {key!r} is invalid ({fields}): {exc.errors()[0]['msg']}") from None
    return projects


def load_registry(path: str | Path | None = None) -> dict[str, ProjectConfig]:
    path = path or os.getenv(PROJECTS_FILE_ENV)
    if not path:
        raise RegistryError(f"{PROJECTS_FILE_ENV} is not set")
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise RegistryError(f"cannot read registry {path}: {exc.strerror}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"registry {path} is not valid YAML: {exc}") from None
    return parse_registry(data)


def read_credential(project: ProjectConfig, secrets_dir: str | Path | None = None, key: str = "token") -> str | None:
    """Return the mounted credential value, or None when it is not mounted."""

    base = Path(secrets_dir or os.getenv(SECRETS_DIR_ENV, DEFAULT_SECRETS_DIR))
    try:
        value = (base / project.secret_name / key).read_text().strip()
    except OSError:
        return None
    return value or None


def main() -> None:
    """Validate a registry file, or a chart values.yaml with a ``projects:`` key.

    ``python projects.py <file>`` prints one line per project;
    ``python projects.py <file> --secrets`` prints all Secret names and
    ``python projects.py <file> <key>...`` the Secret name of each project.
    """

    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        projects = load_registry(path)
    except RegistryError as exc:
        raise SystemExit(f"invalid registry: {exc}")
    if sys.argv[2:] == ["--secrets"]:
        print(" ".join(sorted({p.secret_name for p in projects.values()})))
        return
    if len(sys.argv) > 2:
        for key in sys.argv[2:]:
            if key not in projects:
                raise SystemExit(f"unknown project {key!r}")
            print(projects[key].secret_name)
        return
    for key, project in projects.items():
        print(f"ok  {key:24} {project.deploy_target.kind}/{project.deploy_target.name}  secret={project.secret_name}")


if __name__ == "__main__":
    main()


__all__ = [
    "ProjectConfig",
    "RegistryError",
    "load_registry",
    "parse_registry",
    "read_credential",
]
