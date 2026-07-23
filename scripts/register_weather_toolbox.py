"""Register the weather MCP server as a Foundry toolbox.

A toolbox is a shared, server-side wrapper around the MCP server that Foundry
agents reference by name (used here by the hosted agent, variation 2). Records
the toolbox name + MCP URL in ``.env`` (``WEATHER_TOOLBOX_NAME`` /
``FOUNDRY_TOOLBOX_ENDPOINT``).

Requires ``WEATHER_MCP_URL`` (run ``scripts.deploy_weather_mcp_server`` first).

    python -m scripts.register_weather_toolbox
"""

from __future__ import annotations

from scripts._helpers import env, load_env, project_client, save_env

TOOLBOX_NAME = "weather-tools"


def main() -> None:
    load_env()
    mcp_url = env("WEATHER_MCP_URL", required=True)
    project_endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True).rstrip("/")

    from azure.ai.projects.models import MCPToolboxTool
    from azure.core.exceptions import ResourceNotFoundError

    with project_client() as client:
        # Idempotent: drop any previous toolbox of this name first.
        try:
            client.toolboxes.delete(name=TOOLBOX_NAME)
            print(f"  ↳ removed existing toolbox '{TOOLBOX_NAME}'")
        except ResourceNotFoundError:
            pass

        created = client.toolboxes.create_version(
            name=TOOLBOX_NAME,
            description="Weather MCP server (current weather + forecast).",
            tools=[
                MCPToolboxTool(
                    server_label="weather",
                    server_url=mcp_url,
                    require_approval="never",
                )
            ],
        )
        version = created.version
        print(f"Registered toolbox '{TOOLBOX_NAME}' version {version}")

    toolbox_mcp_url = f"{project_endpoint}/toolboxes/{TOOLBOX_NAME}/versions/{version}/mcp?api-version=v1"
    save_env({"WEATHER_TOOLBOX_NAME": TOOLBOX_NAME, "FOUNDRY_TOOLBOX_ENDPOINT": toolbox_mcp_url})
    print(f"\nToolbox MCP endpoint: {toolbox_mcp_url}")


if __name__ == "__main__":
    main()
