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

from scripts._helpers import ensure_toolbox_role, env, load_env, project_client, save_env, weather_tool_mode

AGENT_NAME = "weather-prompt-agent"
INSTRUCTIONS = (
    "You are a helpful weather assistant. Answer questions about the current "
    "weather and forecast for a city using the available weather tools. Call list_cities "
    "if you are unsure which cities are available. Always call a tool for real "
    "data instead of guessing. Format current-weather answers exactly as: "
    "{\"city\":\"<city>\"}In **<city>, <country>**, it's currently **<temperature>°C** "
    "(feels like **<feels_like>°C**) with **<condition>**.\n"
    "**Humidity:** <humidity>% • **Wind:** <wind> kph • **Precipitation:** <precipitation> mm. "
    "Format forecast answers as: {\"city\":\"<city>\",\"days\":<days>}"
    "**<city>, <country> — next <days> days:** followed by exactly one line per day: "
    "- **<date>:** <condition>, **<temperature>°C** (feels like **<feels_like>°C**), "
    "humidity **<humidity>%**, wind **<wind> kph**, precipitation **<precipitation> mm**. "
    "Do not add an introduction, conclusion, raw tool output, or other commentary."
)


def main() -> None:
    load_env()
    tool_mode = weather_tool_mode()
    tool_url = (
        env("FOUNDRY_TOOLBOX_ENDPOINT", required=True)
        if tool_mode == "toolbox"
        else env("WEATHER_MCP_URL", required=True)
    )
    if tool_mode == "toolbox":
        ensure_toolbox_role()
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
                    MCPTool(server_label="weather", server_url=tool_url, require_approval="never"),
                ],
            ),
            description=f"Weather prompt agent using weather tools via {tool_mode} MCP.",
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

    save_env({"WEATHER_PROMPT_AGENT_ID": AGENT_NAME, "WEATHER_PROMPT_AGENT_TOOL_MODE": tool_mode})
    print(f"\nPrompt agent ready: {AGENT_NAME}")
    print("Invoke via the Foundry Responses API, e.g.:")
    print(f"  client.get_openai_client(agent_name='{AGENT_NAME}').responses.create(input='weather in Berlin?')")
    print("Also exposed: A2A (.../agents/{name}/endpoint/protocols/a2a) and")
    print("invocations (.../agents/{name}/endpoint/protocols/invocations).")


if __name__ == "__main__":
    main()
