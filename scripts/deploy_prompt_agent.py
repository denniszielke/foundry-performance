"""Deploy the prompt agent (variation 1) — a Foundry-native agent.

No container: the agent is just a model + instructions plus an inline MCP tool
pointing straight at the weather MCP server. Foundry hosts it and exposes the
Responses, A2A, and invocations protocols natively (no container needed to
front these — Foundry translates them for any agent definition). Records the
agent name in ``.env`` (``WEATHER_PROMPT_AGENT_ID``).

Requires ``WEATHER_MCP_URL`` (run ``scripts.deploy_weather_mcp_server`` first).

    python -m scripts.deploy_prompt_agent
"""

from __future__ import annotations

from scripts._helpers import env, load_env, project_client, save_env

AGENT_NAME = "weather-prompt-agent"
INSTRUCTIONS = (
    "You are a helpful weather assistant. Answer questions about the current "
    "weather and forecast for a city using the weather tools. Call list_cities "
    "if you are unsure which cities are available. Always call a tool for real "
    "data instead of guessing, and keep answers short."
)


def main() -> None:
    load_env()
    mcp_url = env("WEATHER_MCP_URL", required=True)
    model = env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini")

    from azure.ai.projects.models import (
        A2AProtocolConfiguration,
        AgentEndpointConfig,
        FixedRatioVersionSelectionRule,
        InvocationsProtocolConfiguration,
        MCPTool,
        ProtocolConfiguration,
        PromptAgentDefinition,
        ResponsesProtocolConfiguration,
        VersionSelector,
    )

    with project_client() as client:
        version = client.agents.create_version(
            agent_name=AGENT_NAME,
            definition=PromptAgentDefinition(
                model=model,
                instructions=INSTRUCTIONS,
                tools=[
                    MCPTool(server_label="weather", server_url=mcp_url, require_approval="never"),
                ],
            ),
            description="Weather prompt agent using the weather MCP server directly.",
        )
        print(f"Created prompt agent '{AGENT_NAME}' version {version.version}")

        client.agents.update_details(
            agent_name=AGENT_NAME,
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(
                    version_selection_rules=[
                        FixedRatioVersionSelectionRule(agent_version=version.version, traffic_percentage=100),
                    ]
                ),
                protocol_configuration=ProtocolConfiguration(
                    responses=ResponsesProtocolConfiguration(),
                    a2a=A2AProtocolConfiguration(),
                    invocations=InvocationsProtocolConfiguration(),
                ),
            ),
        )
        print(f"Routed 100% of traffic to version {version.version}")

    save_env({"WEATHER_PROMPT_AGENT_ID": AGENT_NAME})
    print(f"\nPrompt agent ready: {AGENT_NAME}")
    print("Invoke via the Foundry Responses API, e.g.:")
    print(f"  client.get_openai_client(agent_name='{AGENT_NAME}').responses.create(input='weather in Berlin?')")
    print("Also exposed: A2A (.../agents/{name}/endpoint/protocols/a2a) and")
    print("invocations (.../agents/{name}/endpoint/protocols/invocations).")


if __name__ == "__main__":
    main()
