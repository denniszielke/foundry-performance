"""Grant the shared hosted-agent identity its toolbox + observability permissions.

Every Foundry-hosted component in this repo (``hosted_agent_responses``,
``hosted_agent_invocations``) runs as the **AI Services account's
system-assigned managed identity** — there's no separate per-agent Entra
Agent Identity here (see the comments in
``src/hosted_agent_invocations/agent.py`` and
``scripts._helpers.ensure_toolbox_role``). That one identity needs two
grants:

  1. **Toolbox access** — the data-plane ``Foundry User`` role on the AI
     Services account, so the agent can call the project's toolbox MCP
     endpoint (``scripts.register_weather_toolbox``); otherwise the toolbox
     returns HTTP 401. This is already applied inline by
     ``scripts.deploy_hosted_agent_responses`` / ``_invocations`` via
     ``scripts._helpers.ensure_toolbox_role`` — running it again here is a
     harmless, idempotent no-op (useful if the role assignment was ever
     removed, or to re-check it without a full redeploy).
  2. **Observability export** — the Agent 365
     ``Agent365.Observability.OtelWrite`` app role on the tenant's
     ``Agent365Observability`` service principal. This is what a
     genAI-telemetry exporter needs if it authenticates as the identity to
     push spans to the Agent 365 ingestion service (see
     https://aka.ms/foundry-grant-agent-365-permissions). This repo's default
     telemetry path uses Application Insights directly
     (``APPLICATIONINSIGHTS_CONNECTION_STRING``, injected by Foundry) which
     does not need this role — it's granted here defensively so a future
     switch to Agent 365 telemetry export doesn't 403.

Requires: ``az login`` as a **Global Administrator** or **Application
Administrator** in the tenant (needed to create the app role assignment) and
the infrastructure already provisioned (``azd up``, so the AI Services
account and its system-assigned identity exist).

Usage::

    python -m scripts.grant_agent_permissions
    python -m scripts.grant_agent_permissions --skip-observability
    python -m scripts.grant_agent_permissions --skip-toolbox
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from scripts._helpers import account_principal_id, ensure_toolbox_role, load_env

# Well-known, tenant-independent identifier for the Agent 365
# "Agent365.Observability.OtelWrite" app role. See
# https://aka.ms/foundry-grant-agent-365-permissions.
OTEL_WRITE_APP_ROLE_ID = "8f71190c-00c8-461d-a63b-f74abde9ba52"

# Display name of the service principal that defines the observability app role.
OBSERVABILITY_SP_DISPLAY_NAME = "Agent365Observability"

_GRAPH = "https://graph.microsoft.com"


def _az(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run an ``az`` command, capturing stdout/stderr."""
    result = subprocess.run(["az", *args], check=False, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"az {' '.join(args)} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result


def _az_rest_json(method: str, uri: str, body: dict | None = None) -> object:
    """Call ``az rest`` against Microsoft Graph and parse the JSON response."""
    args = ["rest", "--method", method, "--uri", uri, "--headers", "Content-Type=application/json"]
    if body is not None:
        args += ["--body", json.dumps(body)]
    out = _az(*args).stdout.strip()
    return json.loads(out) if out else None


def _resolve_observability_sp_id() -> str:
    """Return the ``Agent365Observability`` service principal object id."""
    uri = (
        f"{_GRAPH}/v1.0/servicePrincipals"
        f"?$filter=displayName eq '{OBSERVABILITY_SP_DISPLAY_NAME}'"
        f"&$select=id,displayName"
    )
    data = _az_rest_json("GET", uri)
    values = (data or {}).get("value", []) if isinstance(data, dict) else []
    if not values:
        raise RuntimeError(
            f"'{OBSERVABILITY_SP_DISPLAY_NAME}' service principal not found in this "
            "tenant. The Agent 365 observability service may not be provisioned — "
            "ask your Microsoft 365 administrator to enable it."
        )
    sp_id = values[0]["id"]
    print(f"  ↳ {OBSERVABILITY_SP_DISPLAY_NAME} service principal: {sp_id}")
    return sp_id


def _assign_otel_write_role(principal_id: str, resource_sp_id: str) -> None:
    """Assign the OtelWrite app role to the account identity (idempotent)."""
    uri = f"{_GRAPH}/v1.0/servicePrincipals/{principal_id}/appRoleAssignments"
    body = {"principalId": principal_id, "resourceId": resource_sp_id, "appRoleId": OTEL_WRITE_APP_ROLE_ID}
    result = _az(
        "rest", "--method", "POST", "--uri", uri,
        "--headers", "Content-Type=application/json",
        "--body", json.dumps(body),
        check=False,
    )
    if result.returncode == 0:
        print(f"  ↳ granted Agent365.Observability.OtelWrite to {principal_id}")
        return
    stderr = result.stderr.lower()
    if "409" in stderr or "conflict" in stderr or "already exists" in stderr:
        print(f"  ↳ already granted for {principal_id} (skipped)")
        return
    raise RuntimeError(
        f"Failed to assign the OtelWrite app role to {principal_id}: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )


def grant_observability_permission() -> None:
    """Grant the Agent 365 OtelWrite app role to the AI account's identity."""
    print("==> Granting Agent 365 observability permission (OtelWrite)")
    principal_id = account_principal_id()
    if not principal_id:
        print("  ↳ AI account has no system-assigned identity; skipping observability role grant")
        return
    resource_sp_id = _resolve_observability_sp_id()
    _assign_otel_write_role(principal_id, resource_sp_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-toolbox", action="store_true", help="Skip the 'Foundry User' toolbox role grant.")
    parser.add_argument(
        "--skip-observability", action="store_true",
        help="Skip the Agent 365 'Agent365.Observability.OtelWrite' app role grant.",
    )
    args = parser.parse_args(argv)

    load_env()

    failures = 0

    if not args.skip_toolbox:
        print("==> Granting toolbox access ('Foundry User' role)")
        try:
            ensure_toolbox_role()
        except Exception as exc:  # noqa: BLE001 - report and continue to the next grant
            print(f"  ERROR: {exc}", file=sys.stderr)
            failures += 1

    if not args.skip_observability:
        try:
            grant_observability_permission()
        except RuntimeError as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            failures += 1

    print("\nDone." if not failures else f"\nDone with {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
