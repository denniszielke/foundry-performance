"""Deploy the external LangChain weather agent as an Azure Container App."""

from __future__ import annotations

import sys

from scripts._helpers import (
    ROOT,
    acr_build,
    deploy_container_app,
    ensure_toolbox_role_for_managed_identity,
    env,
    load_env,
    managed_identity_client_id,
    project_client,
    save_env,
    tag_from_cli,
)

APP_NAME = "weather-custom-agent-langchain"
AGENT_NAME = APP_NAME
OTEL_AGENT_ID = f"{AGENT_NAME}-v1"
PORT = 8088


def register_external_agent() -> None:
    """Register the container as a Foundry external agent for observability."""
    from azure.ai.projects.models import ExternalAgentDefinition

    print(f"\n==> Registering external agent '{AGENT_NAME}' (otel_agent_id={OTEL_AGENT_ID})")
    try:
        with project_client() as client:
            agent = client.agents.create_version(
                agent_name=AGENT_NAME,
                description=(
                    "Custom weather agent (LangChain/LangGraph) hosted as an "
                    "Azure Container App."
                ),
                definition=ExternalAgentDefinition(otel_agent_id=OTEL_AGENT_ID),
            )
        print(f"Registered external agent: {agent.name} (version {agent.version})")
        save_env({"WEATHER_CUSTOM_AGENT_LANGCHAIN_NAME": AGENT_NAME})
    except Exception as exc:  # noqa: BLE001 - registration must not fail the deploy
        print(f"WARNING: external-agent registration failed ({exc}); deployment is unaffected.")


def main(tag: str | None = None, *, register: bool = True) -> None:
    load_env()
    endpoint_type = env("AZURE_AI_ENDPOINT_TYPE", "foundry").lower()

    print("==> Granting the managed identity project access ('Foundry User' role)")
    try:
        ensure_toolbox_role_for_managed_identity()
    except Exception as exc:  # noqa: BLE001 - a missing grant must not block the deploy
        print(f"  WARNING: role grant failed ({exc}); the agent's Foundry calls may 401/403.")

    image = acr_build(
        APP_NAME,
        ROOT / "src" / "custom_agent_langchain" / "Dockerfile",
        tag=tag,
    )
    container_env = {
        "WEATHER_MCP_URL": env("WEATHER_MCP_URL", required=True),
        "AZURE_AI_ENDPOINT_TYPE": endpoint_type,
        "AZURE_AI_PROJECT_ENDPOINT": env("AZURE_AI_PROJECT_ENDPOINT", required=True),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": env(
            "AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"
        ),
        "AZURE_CLIENT_ID": managed_identity_client_id(),
        "OTEL_AGENT_ID": OTEL_AGENT_ID,
        "IMAGE_TAG": image.rsplit(":", 1)[-1],
    }
    if api_version := env("AZURE_OPENAI_API_VERSION"):
        container_env["AZURE_OPENAI_API_VERSION"] = api_version
    url = deploy_container_app(
        APP_NAME,
        image,
        target_port=PORT,
        env_vars=container_env,
        cpu=env("CUSTOM_AGENT_CPU", "1"),
        memory=env("CUSTOM_AGENT_MEMORY", "2.0Gi"),
        readiness_path="/readiness",
    )
    save_env(
        {
            "WEATHER_CUSTOM_AGENT_LANGCHAIN_URL": url,
            "WEATHER_CUSTOM_AGENT_LANGCHAIN_IMAGE": image,
        }
    )

    if register:
        register_external_agent()

    print(f"\nLangChain custom agent ready: {url}")
    print("Benchmark it with:")
    print("  python -m src.clients.run_benchmark --agent custom-langchain")


if __name__ == "__main__":
    main(tag_from_cli(), register="--no-register" not in sys.argv)