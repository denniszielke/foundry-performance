"""Deploy the hosted agent — invocations variation — a Foundry-hosted container.

Builds the AG-UI/Pydantic AI container image and registers it in Foundry as a
``HostedAgentDefinition`` implementing only the plain ``POST /invocations``
protocol, handled by the native ``InvocationAgentServerHost``. The container's
weather tool is served through the **Foundry toolbox**. Foundry pulls the
image, runs it, and fronts the invocations protocol. Records the agent name in ``.env``
(``WEATHER_HOSTED_AGENT_INVOCATIONS_NAME``).

Requires the weather toolbox to be registered (run
``scripts.register_weather_toolbox`` first, which records ``WEATHER_TOOLBOX_NAME``)
and the built image (``scripts.build_containers`` or this script builds it).

    python -m scripts.deploy_hosted_agent_invocations
"""

from __future__ import annotations

import time

from scripts._helpers import (
    ROOT,
    acr_build,
    ensure_openai_role,
    ensure_toolbox_role,
    env,
    load_env,
    project_client,
    save_env,
    tag_from_cli,
)

AGENT_NAME = "weather-hosted-agent-invocations"
# The AG-UI sample implements the Foundry invocations container protocol v2.0.0.
PROTOCOLS = [("invocations", "2.0.0")]


def main(tag: str | None = None) -> None:
    load_env()
    endpoint_type = env("AZURE_AI_ENDPOINT_TYPE", "foundry").lower()
    image = acr_build(AGENT_NAME, ROOT / "src" / "hosted_agent_invocations" / "Dockerfile", tag=tag)

    # The hosted agent runs as the AI account's system-assigned identity; grant it
    # the data-plane 'Foundry User' role so its calls to the toolbox MCP endpoint
    # are authorized (otherwise the toolbox returns HTTP 401).
    ensure_toolbox_role()

    # Foundry reserves the FOUNDRY_* and AGENT_* namespaces plus
    # APPLICATIONINSIGHTS_CONNECTION_STRING for platform use and injects them into
    # the container itself, so they must NOT be supplied here. Pass the toolbox
    # *name* instead; the runner rebuilds the toolbox MCP URL from it and the
    # project endpoint when FOUNDRY_TOOLBOX_ENDPOINT is absent.
    container_env = {
        "WEATHER_TOOLBOX_NAME": env("WEATHER_TOOLBOX_NAME", "weather-tools"),
        "AZURE_AI_ENDPOINT_TYPE": endpoint_type,
        "AZURE_AI_PROJECT_ENDPOINT": env("AZURE_AI_PROJECT_ENDPOINT", required=True),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),
        # Logged at startup so you can tell which build a running version is on
        # (not a reserved FOUNDRY_*/AGENT_* name, so Foundry won't override it).
        "IMAGE_TAG": image.rsplit(":", 1)[-1],
    }
    if api_version := env("AZURE_OPENAI_API_VERSION"):
        container_env["AZURE_OPENAI_API_VERSION"] = api_version

    from azure.ai.projects.models import (
        AgentEndpointConfig,
        ContainerConfiguration,
        FixedRatioVersionSelectionRule,
        HostedAgentDefinition,
        InvocationsProtocolConfiguration,
        ProtocolConfiguration,
        ProtocolVersionRecord,
        VersionSelector,
    )

    with project_client() as client:
        created = client.agents.create_version(
            agent_name=AGENT_NAME,
            definition=HostedAgentDefinition(
                cpu=env("HOSTED_AGENT_CPU", "1"),
                memory=env("HOSTED_AGENT_MEMORY", "2Gi"),
                container_configuration=ContainerConfiguration(image=image),
                environment_variables=container_env,
                protocol_versions=[ProtocolVersionRecord(protocol=p, version=v) for p, v in PROTOCOLS],
            ),
            description="Weather agent (Pydantic AI AG-UI) hosted in Foundry, invocations protocol only.",
            metadata={"enableVnextExperience": "true"},
            headers={"Foundry-Features": "HostedAgents=V1Preview"},
        )
        print(f"Created hosted agent '{AGENT_NAME}' version {created.version}; waiting for it to become Active…")

        deadline = time.time() + 900
        while time.time() < deadline:
            current = client.agents.get_version(agent_name=AGENT_NAME, agent_version=created.version)
            status = getattr(current, "status", None)
            status_value = getattr(status, "value", status)
            print(f"  status={status}")
            if status_value == "active":
                break
            if status_value in ("failed", "deleting", "deleted"):
                raise SystemExit(f"Hosted agent version entered terminal status: {status}")
            time.sleep(15)
        else:
            raise SystemExit("Timed out waiting for the hosted agent to become Active.")

        if endpoint_type == "openai":
            instance_identity = getattr(current, "instance_identity", None)
            ensure_openai_role(getattr(instance_identity, "principal_id", ""))

        client.agents.update_details(
            agent_name=AGENT_NAME,
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(
                    version_selection_rules=[
                        FixedRatioVersionSelectionRule(agent_version=created.version, traffic_percentage=100),
                    ]
                ),
                protocol_configuration=ProtocolConfiguration(
                    invocations=InvocationsProtocolConfiguration(),
                ),
            ),
        )
        print(f"Routed 100% of traffic to version {created.version}")

    save_env({"WEATHER_HOSTED_AGENT_INVOCATIONS_NAME": AGENT_NAME, "WEATHER_HOSTED_AGENT_INVOCATIONS_IMAGE": image})
    print(f"\nHosted agent (invocations) ready: {AGENT_NAME} ({image})")


if __name__ == "__main__":
    main(tag_from_cli())
