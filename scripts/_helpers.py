"""Shared helpers for the deployment scripts.

Thin wrappers around the Azure CLI (`az acr build`, `az deployment group
create`, `az containerapp show`) plus a small `.env` reader/writer so each
deploy script stays short and the wiring between steps (URLs, agent ids) is
captured back into `./.env` for the next step and the benchmark clients.

All scripts are run from the repository root, e.g.::

    python -m scripts.deploy_weather_mcp_server
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
APP_BICEP = ROOT / "infra" / "core" / "host" / "app.bicep"


# --------------------------------------------------------------------------- #
# .env handling
# --------------------------------------------------------------------------- #
def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Read ``.env`` into a dict and merge it into ``os.environ`` (no override)."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            values[key] = value
            os.environ.setdefault(key, value)
    return values


def save_env(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Insert/replace ``KEY=value`` lines in ``.env`` (creates it if missing)."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value
    print(f"  ↳ wrote {', '.join(updates)} to {path.name}")


def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default if default is not None else "")
    if required and not value:
        sys.exit(f"ERROR: required environment variable {key} is not set (run `azd up` first).")
    return value


# --------------------------------------------------------------------------- #
# Azure CLI wrappers
# --------------------------------------------------------------------------- #
def run(cmd: list[str], *, capture: bool = False) -> str:
    """Run a command, echoing it. Returns stdout when ``capture`` is True."""
    print("+ " + " ".join(cmd))
    result = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return (result.stdout or "").strip() if capture else ""


def registry_name() -> str:
    """Bare ACR name (e.g. ``myregistry``), derived from env if needed."""
    name = env("AZURE_CONTAINER_REGISTRY_NAME")
    if name:
        return name
    endpoint = env("AZURE_CONTAINER_REGISTRY_ENDPOINT") or env("AZURE_REGISTRY", required=True)
    return endpoint.split(".", 1)[0]


def container_env_default_domain() -> str:
    """Default domain of the Container Apps environment (for FQDN prediction)."""
    return run(
        [
            "az", "containerapp", "env", "show",
            "--name", env("AZURE_CONTAINER_ENVIRONMENT_NAME", required=True),
            "--resource-group", env("AZURE_RESOURCE_GROUP", required=True),
            "--query", "properties.defaultDomain",
            "-o", "tsv",
        ],
        capture=True,
    )


def acr_build(image: str, dockerfile: Path, context: Path = ROOT, *, tag: str = "latest") -> str:
    """Build+push an image in ACR (`az acr build`). Returns the full image ref."""
    registry = registry_name()
    reference = f"{image}:{tag}"
    run([
        "az", "acr", "build",
        "--registry", registry,
        "--image", reference,
        "--file", str(dockerfile),
        str(context),
    ])
    login_server = env("AZURE_CONTAINER_REGISTRY_ENDPOINT") or f"{registry}.azurecr.io"
    full = f"{login_server}/{reference}"
    print(f"  ↳ built {full}")
    return full


def deploy_container_app(
    name: str,
    image: str,
    *,
    target_port: int,
    env_vars: dict[str, str],
    readiness_path: str = "",
    min_replicas: int = 0,
    external: bool = True,
) -> str:
    """Deploy the app.bicep Container App module and return its https URL."""
    resource_group = env("AZURE_RESOURCE_GROUP", required=True)
    env_json = json.dumps([{"name": k, "value": v} for k, v in env_vars.items()])
    params = [
        f"name={name}",
        f"imageName={image}",
        f"targetPort={target_port}",
        f"containerAppsEnvironmentName={env('AZURE_CONTAINER_ENVIRONMENT_NAME', required=True)}",
        f"containerRegistryName={registry_name()}",
        f"identityName={env('AZURE_MANAGED_IDENTITY_NAME', required=True)}",
        f"envJson={env_json}",
        f"readinessProbePath={readiness_path}",
        f"minReplicas={min_replicas}",
        f"external={'true' if external else 'false'}",
    ]
    uri = run(
        [
            "az", "deployment", "group", "create",
            "--resource-group", resource_group,
            "--name", f"deploy-{name}",
            "--template-file", str(APP_BICEP),
            "--parameters", *params,
            "--query", "properties.outputs.uri.value",
            "-o", "tsv",
        ],
        capture=True,
    )
    print(f"  ↳ {name} available at {uri}")
    return uri


# --------------------------------------------------------------------------- #
# Foundry project client
# --------------------------------------------------------------------------- #
def project_client() -> Any:
    """Construct an ``AIProjectClient`` for the provisioned Foundry project."""
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True)
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
