"""Deploy the hosted agent (variation 2) — a Foundry-hosted container.

Builds the agent-framework container image and registers it in Foundry as a
``HostedAgentDefinition``. The container's weather tool is served through the
**Foundry toolbox** (``WEATHER_TOOL_MODE=toolbox``); Foundry pulls the image,
runs it, and front-ends its protocols. Records the agent name in ``.env``
(``WEATHER_HOSTED_AGENT_NAME``).

Requires ``FOUNDRY_TOOLBOX_ENDPOINT`` (run ``scripts.register_weather_toolbox``
first) and the built image (``scripts.build_containers`` or this script builds it).

    python -m scripts.deploy_hosted_agent
"""

from __future__ import annotations

import time

from scripts._helpers import ROOT, acr_build, env, load_env, project_client, save_env

AGENT_NAME = "weather-hosted-agent"
# Foundry-fronted protocols for this hosted agent. The container additionally
# serves invocations_ws / activity for the direct-comparison benchmark, but only
# the protocols listed here are exposed through the Foundry endpoint. Trim this
# to what the target Foundry region supports if create_version rejects one.
PROTOCOLS = [("responses", "1.0.0"), ("a2a", "1.0.0")]


def main() -> None:
    load_env()
    image = acr_build(AGENT_NAME, ROOT / "src" / "hosted_agent" / "Dockerfile")

    container_env = {
        "WEATHER_TOOL_MODE": "toolbox",
        "FOUNDRY_TOOLBOX_ENDPOINT": env("FOUNDRY_TOOLBOX_ENDPOINT", required=True),
        "AZURE_AI_PROJECT_ENDPOINT": env("AZURE_AI_PROJECT_ENDPOINT", required=True),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),
    }
    conn = env("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn:
        container_env["APPLICATIONINSIGHTS_CONNECTION_STRING"] = conn

    from azure.ai.projects.models import (
        AgentEndpointConfig,
        ContainerConfiguration,
        FixedRatioVersionSelectionRule,
        HostedAgentDefinition,
        ProtocolConfiguration,
        ProtocolVersionRecord,
        ResponsesProtocolConfiguration,
        VersionSelector,
    )

    with project_client() as client:
        created = client.agents.create_version(
            agent_name=AGENT_NAME,
            definition=HostedAgentDefinition(
                cpu="1",
                memory="2Gi",
                container_configuration=ContainerConfiguration(image=image),
                environment_variables=container_env,
                protocol_versions=[ProtocolVersionRecord(protocol=p, version=v) for p, v in PROTOCOLS],
            ),
            description="Weather agent (Agent Framework) hosted in Foundry, tool via toolbox.",
        )
        print(f"Created hosted agent '{AGENT_NAME}' version {created.version}; waiting for it to become Active…")

        deadline = time.time() + 900
        while time.time() < deadline:
            current = client.agents.get_version(agent_name=AGENT_NAME, agent_version=created.version)
            status = getattr(current, "status", None)
            print(f"  status={status}")
            if status == "Active":
                break
            if status in ("Failed", "Canceled"):
                raise SystemExit(f"Hosted agent version entered terminal status: {status}")
            time.sleep(15)
        else:
            raise SystemExit("Timed out waiting for the hosted agent to become Active.")

        client.agents.update_details(
            agent_name=AGENT_NAME,
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(
                    version_selection_rules=[
                        FixedRatioVersionSelectionRule(agent_version=created.version, traffic_percentage=100),
                    ]
                ),
                protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration()),
            ),
        )
        print(f"Routed 100% of traffic to version {created.version}")

    save_env({"WEATHER_HOSTED_AGENT_NAME": AGENT_NAME})
    print(f"\nHosted agent ready: {AGENT_NAME}")


if __name__ == "__main__":
    main()
