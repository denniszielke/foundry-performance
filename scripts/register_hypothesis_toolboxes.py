"""Register the three scenario MCP services as Foundry toolboxes."""

from __future__ import annotations

from scripts._helpers import env, load_env, project_client, save_env

TOOLBOXES = {
    "INTERNET_RESEARCH": ("internet-research", "Current public internet research"),
    "CONTEXT_API": ("context-api", "Structured scenario context API retrieval"),
    "DOCUMENT_SEARCH": ("document-search", "Approved enterprise document search"),
}


def main() -> None:
    load_env()
    project_endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True).rstrip("/")
    test_mcp_url = env("WEATHER_MCP_URL", required=True)

    from azure.ai.projects.models import MCPToolboxTool
    from azure.core.exceptions import ResourceNotFoundError

    updates: dict[str, str] = {}
    with project_client() as client:
        for prefix, (default_name, description) in TOOLBOXES.items():
            name = env(f"{prefix}_TOOLBOX_NAME", default_name)
            mcp_url = env(f"{prefix}_MCP_URL") or test_mcp_url
            if not env(f"{prefix}_MCP_URL"):
                print(f"  ↳ {prefix}_MCP_URL is unset; using WEATHER_MCP_URL for testing")
            try:
                client.toolboxes.delete(name=name)
                print(f"  ↳ removed existing toolbox '{name}'")
            except ResourceNotFoundError:
                pass
            created = client.toolboxes.create_version(
                name=name,
                description=description,
                tools=[
                    MCPToolboxTool(
                        server_label=default_name,
                        server_url=mcp_url,
                        require_approval="never",
                    )
                ],
            )
            endpoint = f"{project_endpoint}/toolboxes/{name}/versions/{created.version}/mcp?api-version=v1"
            updates[f"{prefix}_TOOLBOX_NAME"] = name
            updates[f"{prefix}_TOOLBOX_ENDPOINT"] = endpoint
            print(f"Registered toolbox '{name}' version {created.version}")

    save_env(updates)


if __name__ == "__main__":
    main()