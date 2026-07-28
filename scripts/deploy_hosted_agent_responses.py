"""Deploy the hosted agent — responses variation — a Foundry-hosted container.

Builds the agent-framework container image and registers it in Foundry as a
``HostedAgentDefinition`` implementing only the responses protocol. The
container's weather tool is served through the **Foundry toolbox**
(``WEATHER_TOOL_MODE=toolbox``); Foundry pulls the image, runs it, and
front-ends the responses protocol plus native A2A. Records the agent name in
``.env`` (``WEATHER_HOSTED_AGENT_RESPONSES_NAME``).

Requires the weather toolbox to be registered (run
``scripts.register_weather_toolbox`` first, which records ``WEATHER_TOOLBOX_NAME``)
and the built image (``scripts.build_containers`` or this script builds it).

    python -m scripts.deploy_hosted_agent_responses
"""

from __future__ import annotations

import time

from scripts._helpers import (
    ROOT,
    acr_build,
    ensure_toolbox_role,
    env,
    load_env,
    project_client,
    save_env,
    tag_from_cli,
)

AGENT_NAME = "weather-hosted-agent-responses"
# agent-framework-foundry-hosting's ResponsesHostServer requires the v2.0.0
# container protocol (it reads context.platform_context.call_id, which only
# exists on 2.0.0) — see the "protocol 1.0.0 ... requires protocol 2.0.0"
# RuntimeError raised by _handle_response on 1.0.0.
PROTOCOLS = [("responses", "2.0.0")]


def main(tag: str | None = None) -> None:
    load_env()
    image = acr_build(AGENT_NAME, ROOT / "src" / "hosted_agent_responses" / "Dockerfile", tag=tag)

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
        "AZURE_AI_PROJECT_ENDPOINT": env("AZURE_AI_PROJECT_ENDPOINT", required=True),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),
        # Logged at startup so you can tell which build a running version is on
        # (not a reserved FOUNDRY_*/AGENT_* name, so Foundry won't override it).
        "IMAGE_TAG": image.rsplit(":", 1)[-1],
    }

    from azure.ai.projects.models import (
        A2AProtocolConfiguration,
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
            description="Weather agent (Agent Framework) hosted in Foundry, responses protocol only.",
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

        client.agents.update_details(
            agent_name=AGENT_NAME,
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(
                    version_selection_rules=[
                        FixedRatioVersionSelectionRule(agent_version=created.version, traffic_percentage=100),
                    ]
                ),
                # Foundry fronts A2A natively for hosted agents that implement the
                # responses protocol (this one does) — no in-container a2a app
                # needed. See:
                # https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint
                protocol_configuration=ProtocolConfiguration(
                    responses=ResponsesProtocolConfiguration(),
                    a2a=A2AProtocolConfiguration(),
                ),
            ),
        )
        print(f"Routed 100% of traffic to version {created.version}")

    save_env({"WEATHER_HOSTED_AGENT_RESPONSES_NAME": AGENT_NAME, "WEATHER_HOSTED_AGENT_RESPONSES_IMAGE": image})
    print(f"\nHosted agent (responses) ready: {AGENT_NAME} ({image})")


if __name__ == "__main__":
    main(tag_from_cli())
