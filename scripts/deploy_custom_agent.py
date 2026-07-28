"""Deploy the custom weather agent (variation 3) as an Azure Container App.

This is the "native hosting, outside Foundry" comparison point: the container
serves every protocol itself (responses / invocations / invocations_ws / a2a /
activity) and calls the weather MCP server **directly** (no toolbox, no auth).
It still calls the Foundry project directly (chat completions via
``FoundryChatClient``) as the repo's user-assigned managed identity, which
needs the ``Foundry User`` role on the AI account — granted here via
``ensure_toolbox_role_for_managed_identity()`` (same role/mechanism as the
hosted agents' toolbox grant, just for a different identity).

After the Container App is deployed the agent is registered in Foundry as an
**external agent** (``ExternalAgentDefinition``) so its telemetry shows up in the
Foundry trace view. Foundry matches spans to the registration by
``gen_ai.agent.id == otel_agent_id``; the container stamps that same
``OTEL_AGENT_ID`` on every span (see ``WeatherAgentRunner``).

Requires ``WEATHER_MCP_URL`` (run ``scripts.deploy_weather_mcp_server`` first).

    python -m scripts.deploy_custom_agent                # deploy + register
    python -m scripts.deploy_custom_agent --no-register  # skip registration
"""

from __future__ import annotations

import sys

from scripts._helpers import (
    ROOT,
    acr_build,
    container_env_default_domain,
    deploy_container_app,
    ensure_toolbox_role_for_managed_identity,
    env,
    load_env,
    managed_identity_client_id,
    project_client,
    save_env,
    tag_from_cli,
)

APP_NAME = "weather-custom-agent"
PORT = 8088

# External-agent identity. AGENT_NAME is the Foundry registration name and
# OTEL_AGENT_ID is stamped on every span as gen_ai.agent.id; the registration's
# otel_agent_id must match it. Defined once here so the container env and the
# registration call below can't drift apart.
AGENT_NAME = "weather-custom-agent"
OTEL_AGENT_ID = f"{AGENT_NAME}-v1"


def register_external_agent() -> None:
    """Register the container as a Foundry external agent for observability.

    Foundry matches incoming spans to this registration by
    ``gen_ai.agent.id == otel_agent_id``; we pass the same ``OTEL_AGENT_ID`` the
    container stamps on every span. ``create_version`` is idempotent for an
    existing name (it adds a revision), so re-running a deploy is safe.
    """
    from azure.ai.projects.models import ExternalAgentDefinition

    print(f"\n==> Registering external agent '{AGENT_NAME}' (otel_agent_id={OTEL_AGENT_ID})")
    try:
        with project_client() as client:
            agent = client.agents.create_version(
                agent_name=AGENT_NAME,
                description="Custom weather agent (Agent Framework) hosted as an Azure Container App.",
                definition=ExternalAgentDefinition(otel_agent_id=OTEL_AGENT_ID),
            )
        print(f"Registered external agent: {agent.name} (version {agent.version})")
        print(f"Resolved otel_agent_id: {agent.definition.otel_agent_id}")
        save_env({"WEATHER_CUSTOM_AGENT_NAME": AGENT_NAME})
    except Exception as exc:  # noqa: BLE001 - registration must not fail the deploy
        print(f"WARNING: external-agent registration failed ({exc}); deployment is unaffected.")


def main(tag: str | None = None, *, register: bool = True) -> None:
    load_env()

    print("==> Granting the managed identity toolbox/project access ('Foundry User' role)")
    try:
        ensure_toolbox_role_for_managed_identity()
    except Exception as exc:  # noqa: BLE001 - a missing grant must not block the deploy
        print(f"  WARNING: role grant failed ({exc}); the agent's Foundry calls may 401/403.")

    image = acr_build(APP_NAME, ROOT / "src" / "custom_agent" / "Dockerfile", tag=tag)

    public_url = f"https://{APP_NAME}.{container_env_default_domain()}"
    container_env = {
        "WEATHER_MCP_URL": env("WEATHER_MCP_URL", required=True),
        "AZURE_AI_PROJECT_ENDPOINT": env("AZURE_AI_PROJECT_ENDPOINT", required=True),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),
        "PUBLIC_BASE_URL": public_url,
        # Only a user-assigned identity is attached to this Container App, so
        # DefaultAzureCredential's ManagedIdentityCredential needs AZURE_CLIENT_ID
        # to know which identity to request a token for (otherwise it tries a
        # system-assigned lookup and fails with "invalid_scope").
        "AZURE_CLIENT_ID": managed_identity_client_id(),
        # Stamp a stable gen_ai.agent.id so the Foundry external-agent
        # registration (otel_agent_id) can match this container's traces.
        "OTEL_AGENT_ID": OTEL_AGENT_ID,
        # Logged at startup so you can tell which build is actually running.
        "IMAGE_TAG": image.rsplit(":", 1)[-1],
    }
    conn = env("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn:
        container_env["APPLICATIONINSIGHTS_CONNECTION_STRING"] = conn

    url = deploy_container_app(
        APP_NAME,
        image,
        target_port=PORT,
        env_vars=container_env,
        readiness_path="/readiness",
    )
    save_env({"WEATHER_CUSTOM_AGENT_URL": url, "WEATHER_CUSTOM_AGENT_IMAGE": image})

    if register:
        register_external_agent()

    print(f"\nCustom agent ready: {url}")
    print("Benchmark it with:")
    print(f"  python -m src.clients.run_benchmark --base-url {url}")


if __name__ == "__main__":
    main(tag_from_cli(), register="--no-register" not in sys.argv)
