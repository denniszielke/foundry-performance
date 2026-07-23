"""Custom weather agent (variation 3) — Azure Container App, outside Foundry.

Microsoft Agent Framework agent whose weather tool connects **directly to the
MCP server** (``WEATHER_TOOL_MODE=direct``, no toolbox, no auth). It serves every
protocol itself from a plain container using the native agent-framework / a2a
hosting and the ``azure-ai-agentserver`` protocol hosts.

Run locally from the project root::

    WEATHER_TOOL_MODE=direct WEATHER_MCP_URL=http://127.0.0.1:8093/mcp \
        python -m src.custom_agent.agent
"""

from __future__ import annotations

from src.agent_common.multiprotocol import run

if __name__ == "__main__":
    run(mode="direct")
