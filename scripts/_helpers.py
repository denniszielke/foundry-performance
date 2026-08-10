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


def weather_tool_mode() -> str:
    """Return the validated weather tool route used by deployed agents."""
    mode = env("WEATHER_TOOL_MODE", "direct").strip().lower()
    if mode not in {"direct", "toolbox"}:
        sys.exit("ERROR: WEATHER_TOOL_MODE must be 'direct' or 'toolbox'.")
    return mode


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


def storage_account_name() -> str:
    """Return the workflow storage account, discovering it for older azd environments."""
    if name := env("AZURE_STORAGE_ACCOUNT_NAME"):
        return name
    output = run(
        [
            "az", "storage", "account", "list",
            "--resource-group", env("AZURE_RESOURCE_GROUP", required=True),
            "--subscription", env("AZURE_SUBSCRIPTION_ID", required=True),
            "--query", "[].name", "-o", "tsv",
        ],
        capture=True,
    )
    names = [item.strip() for item in output.splitlines() if item.strip()]
    if len(names) != 1:
        sys.exit(
            "ERROR: AZURE_STORAGE_ACCOUNT_NAME is not set and storage discovery "
            f"found {len(names)} accounts in the resource group. Set it explicitly."
        )
    save_env({"AZURE_STORAGE_ACCOUNT_NAME": names[0]})
    return names[0]


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
    cpu: str = "1",
    memory: str = "2.0Gi",
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
        f"containerCpuCoreCount={cpu}",
        f"containerMemory={memory}",
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
# "Foundry User" data-plane role. Both the AI account identity and each hosted
# agent version's instance identity may need it for project toolbox calls.
FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"


def _account_resource_id() -> str:
    """Resource id of the AI Services account (derived from the project id)."""
    project_id = env("AZURE_AI_PROJECT_ID", required=True)
    return project_id.split("/projects/", 1)[0]


def account_principal_id() -> str:
    """System-assigned managed identity principal id of the AI Services account.

    This account-level identity remains the target for shared services and
    observability grants. Hosted versions also expose an instance identity,
    which deployment scripts grant separately after the version is active.
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


def ensure_openai_role(principal_id: str) -> None:
    """Grant a hosted agent instance identity direct Azure OpenAI inference access."""
    if not principal_id:
        print("  ↳ hosted agent has no instance identity; skipping OpenAI role grant")
        return
    _grant_account_role(principal_id, "ServicePrincipal", COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID)
    print("  ↳ OpenAI role (Cognitive Services OpenAI User) ready for the hosted agent identity")


def ensure_toolbox_role_for_hosted_agent(principal_id: str) -> None:
    """Grant a hosted agent version's instance identity access to Foundry toolboxes."""
    if not principal_id:
        print("  ↳ hosted agent has no instance identity; skipping toolbox role grant")
        return
    _grant_toolbox_role(principal_id, "ServicePrincipal")
    print("  ↳ toolbox role (Foundry User) ready for the hosted agent identity")


def ensure_storage_blob_role(principal_id: str | None = None) -> None:
    """Grant an account or hosted-agent identity access to the workflow blob store."""
    identity_label = "hosted agent identity" if principal_id else "account identity"
    principal_id = principal_id or account_principal_id()
    if not principal_id:
        print(f"  ↳ {identity_label} unavailable; skipping storage role grant")
        return
    storage_id = run(
        [
            "az", "storage", "account", "show",
            "--name", storage_account_name(),
            "--resource-group", env("AZURE_RESOURCE_GROUP", required=True),
            "--query", "id", "-o", "tsv",
        ],
        capture=True,
    )
    existing = run(
        [
            "az", "role", "assignment", "list",
            "--assignee", principal_id,
            "--scope", storage_id,
            "--role", STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
            "--query", "[].id", "-o", "tsv",
        ],
        capture=True,
    )
    if not existing:
        run([
            "az", "role", "assignment", "create",
            "--assignee-object-id", principal_id,
            "--assignee-principal-type", "ServicePrincipal",
            "--role", STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
            "--scope", storage_id,
        ])
    print(f"  ↳ Storage Blob Data Contributor ready for the {identity_label}")


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


def managed_identity_principal_id(identity_name: str | None = None) -> str:
    """Principal id of the repo's user-assigned managed identity (``id-*``).

    This is the identity Container Apps use for ACR pull (see
    ``AZURE_MANAGED_IDENTITY_NAME``); some agents (e.g. the custom_agent_maf
    container) also run their Foundry calls as this identity, so it needs the
    same toolbox/project role as the AI account's system identity.
    """
    name = identity_name or env("AZURE_MANAGED_IDENTITY_NAME", required=True)
    resource_group = env("AZURE_RESOURCE_GROUP", required=True)
    return run(
        ["az", "identity", "show", "--resource-group", resource_group,
         "--name", name, "--query", "principalId", "-o", "tsv"],
        capture=True,
    )


def managed_identity_client_id(identity_name: str | None = None) -> str:
    """Client id of the repo's user-assigned managed identity (``id-*``).

    A Container App with ONLY a user-assigned identity attached does not set
    ``AZURE_CLIENT_ID`` for it automatically — ``DefaultAzureCredential``'s
    ``ManagedIdentityCredential`` then tries to resolve a system-assigned
    identity by default and fails with ``ManagedIdentityCredential: ... Token
    request error: (invalid_scope) 400``. Stamp this value as the container's
    ``AZURE_CLIENT_ID`` env var so it picks the right (user-assigned) identity.
    """
    name = identity_name or env("AZURE_MANAGED_IDENTITY_NAME", required=True)
    resource_group = env("AZURE_RESOURCE_GROUP", required=True)
    return run(
        ["az", "identity", "show", "--resource-group", resource_group,
         "--name", name, "--query", "clientId", "-o", "tsv"],
        capture=True,
    )


def ensure_toolbox_role_for_managed_identity(identity_name: str | None = None) -> None:
    """Grant the repo's user-assigned managed identity the ``Foundry User`` role.

    Idempotent. Needed by any Container App that authenticates its own Foundry
    calls (chat completions, toolbox MCP, etc.) as this identity rather than
    the AI account's system-assigned identity — otherwise those calls fail
    with 401/403 even though the container itself starts up fine.
    """
    principal_id = managed_identity_principal_id(identity_name)
    if not principal_id:
        print("  ↳ managed identity not found; skipping toolbox role grant")
        return
    _grant_toolbox_role(principal_id, "ServicePrincipal")
    print("  ↳ toolbox role (Foundry User) ready for the managed identity")


def _grant_toolbox_role(principal_id: str, principal_type: str) -> None:
    """Idempotently assign the ``Foundry User`` role to ``principal_id`` on the AI account."""
    _grant_account_role(principal_id, principal_type, FOUNDRY_USER_ROLE_ID)


def _grant_account_role(principal_id: str, principal_type: str, role_id: str) -> None:
    """Idempotently assign an RBAC role to ``principal_id`` on the AI account."""
    account_id = _account_resource_id()
    existing = run(
        ["az", "role", "assignment", "list",
         "--assignee", principal_id, "--scope", account_id,
         "--role", role_id, "--query", "[].id", "-o", "tsv"],
        capture=True,
    )
    if existing:
        return
    run(["az", "role", "assignment", "create",
         "--assignee-object-id", principal_id,
         "--assignee-principal-type", principal_type,
         "--role", role_id, "--scope", account_id])
