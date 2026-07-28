"""Shared helpers for the deployment scripts.

Thin wrappers around the Azure CLI (`az acr build`, `az deployment group
create`, `az containerapp show`) plus a small `.env` reader/writer so each
deploy script stays short and the wiring between steps (URLs, agent ids) is
captured back into `./.env` for the next step and the benchmark clients.

All scripts are run from the repository root, e.g.::

    python -m scripts.deploy_weather_mcp_server
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _generated_tag() -> str:
    """A unique, traceable image tag for this process: ``<git-sha>-<utc-ts>``.

    Cached so every image built in a single run shares one tag. Falls back to a
    bare UTC timestamp when git metadata is unavailable.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        sha = proc.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        sha = ""
    return f"{sha}-{ts}" if sha else ts


def resolve_image_tag(explicit: str | None = None) -> str:
    """Resolve the image tag to build/deploy.

    Precedence: explicit argument (e.g. ``--tag``) → ``IMAGE_TAG`` env → a unique
    generated ``<git-sha>-<utc-ts>`` tag. Never returns ``latest`` so each build
    is uniquely addressable and deployments are reproducible.
    """
    return explicit or os.getenv("IMAGE_TAG") or _generated_tag()


def tag_from_cli(description: str = "Build and deploy a uniquely-tagged image.") -> str | None:
    """Parse an optional ``--tag/-t`` from argv (returns ``None`` when omitted)."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--tag", "-t", default=None,
        help="Custom image tag to build and deploy (default: IMAGE_TAG env or a generated unique tag).",
    )
    args, _ = parser.parse_known_args()
    return args.tag


def acr_build(image: str, dockerfile: Path, context: Path = ROOT, *, tag: str | None = None) -> str:
    """Build+push an image in ACR (`az acr build`). Returns the full image ref.

    ``tag`` defaults to :func:`resolve_image_tag` (a unique ``<git-sha>-<utc-ts>``
    tag, or ``IMAGE_TAG`` when set) so images are never overwritten under
    ``latest``.
    """
    registry = registry_name()
    reference = f"{image}:{resolve_image_tag(tag)}"
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
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential(), allow_preview=True)


# --------------------------------------------------------------------------- #
# Toolbox RBAC (hosted agent → Foundry toolbox)
# --------------------------------------------------------------------------- #
# "Foundry User" data-plane role. A Foundry-hosted agent runs as the AI account's
# system-assigned identity; without this role its calls to project toolboxes are
# rejected with HTTP 401.
FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"


def _account_resource_id() -> str:
    """Resource id of the AI Services account (derived from the project id)."""
    project_id = env("AZURE_AI_PROJECT_ID", required=True)
    return project_id.split("/projects/", 1)[0]


def account_principal_id() -> str:
    """System-assigned managed identity principal id of the AI Services account.

    This is the single identity every Foundry-hosted component in this repo
    runs as (there's no per-agent Entra Agent Identity here), so it's the
    target for both the toolbox role and the observability app role grants.
    Returns ``""`` if the account has no system-assigned identity.
    """
    account_id = _account_resource_id()
    return run(
        ["az", "resource", "show", "--ids", account_id,
         "--query", "identity.principalId", "-o", "tsv"],
        capture=True,
    )


def ensure_toolbox_role() -> None:
    """Grant the AI account's system-assigned identity the ``Foundry User`` role.

    Idempotent. This is what lets a Foundry-hosted agent authenticate to the
    project's toolbox MCP endpoint (otherwise the toolbox returns HTTP 401).
    """
    principal_id = account_principal_id()
    if not principal_id:
        print("  ↳ AI account has no system-assigned identity; skipping toolbox role grant")
        return
    _grant_toolbox_role(principal_id, "ServicePrincipal")
    print("  ↳ toolbox role (Foundry User) ready for the account identity")


def signed_in_user_principal_id() -> str:
    """Object id of the currently ``az login``-ed user."""
    return run(["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"], capture=True)


def ensure_toolbox_role_for_signed_in_user() -> None:
    """Grant the current ``az login``-ed user the ``Foundry User`` role.

    Idempotent. Lets you run a hosted-agent container's ``agent.py`` module
    directly on your machine (``python -m src.hosted_agent_responses.agent``)
    against the real toolbox for local testing — the deployed container runs
    as the AI account's managed identity (see ``ensure_toolbox_role``), which
    your local ``DefaultAzureCredential`` (via ``az login``) does not use.
    """
    principal_id = signed_in_user_principal_id()
    if not principal_id:
        print("  ↳ no signed-in az CLI user found (run `az login`); skipping toolbox role grant")
        return
    _grant_toolbox_role(principal_id, "User")
    print("  ↳ toolbox role (Foundry User) ready for the signed-in user (local testing)")


def _grant_toolbox_role(principal_id: str, principal_type: str) -> None:
    """Idempotently assign the ``Foundry User`` role to ``principal_id`` on the AI account."""
    account_id = _account_resource_id()
    existing = run(
        ["az", "role", "assignment", "list",
         "--assignee", principal_id, "--scope", account_id,
         "--role", FOUNDRY_USER_ROLE_ID, "--query", "[].id", "-o", "tsv"],
        capture=True,
    )
    if existing:
        return
    run(["az", "role", "assignment", "create",
         "--assignee-object-id", principal_id,
         "--assignee-principal-type", principal_type,
         "--role", FOUNDRY_USER_ROLE_ID, "--scope", account_id])
