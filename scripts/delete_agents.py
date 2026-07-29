"""Delete the Foundry agents and toolbox created for the scenario.

Removes the prompt agent, all hosted agent variations, and the weather toolbox.
The Container Apps (weather MCP server + custom agents) are part of
the resource group and are removed by ``azd down``; this script only cleans
the Foundry management-plane objects.

    python -m scripts.delete_agents
"""

from __future__ import annotations

from scripts._helpers import load_env, project_client

AGENT_NAMES = [
    "weather-prompt-agent",
    "weather-hosted-agent-responses",
    "weather-hosted-agent-invocations",
    "weather-custom-agent-maf",
    "weather-custom-agent-langchain",
]
TOOLBOX_NAMES = ["weather-tools"]


def main() -> None:
    load_env()
    from azure.core.exceptions import ResourceNotFoundError

    with project_client() as client:
        for name in AGENT_NAMES:
            try:
                client.agents.delete(agent_name=name, force=True)
                print(f"Deleted agent '{name}'")
            except ResourceNotFoundError:
                print(f"Agent '{name}' not found, skipping")

        for name in TOOLBOX_NAMES:
            try:
                client.toolboxes.delete(name=name)
                print(f"Deleted toolbox '{name}'")
            except ResourceNotFoundError:
                print(f"Toolbox '{name}' not found, skipping")

    print("\nFoundry cleanup complete. Run `azd down` to remove the Azure resources.")


if __name__ == "__main__":
    main()
