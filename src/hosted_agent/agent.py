"""Hosted weather agent (variation 2) — Foundry-hosted container.

Microsoft Agent Framework agent whose weather tool is served through the
**Foundry toolbox** (``WEATHER_TOOL_MODE=toolbox``). Registered in Foundry as a
hosted agent and exposed by the platform over responses / a2a / invocations.

Run locally from the project root::

    WEATHER_TOOL_MODE=toolbox python -m src.hosted_agent.agent
"""

from __future__ import annotations

from src.agent_common.multiprotocol import run

if __name__ == "__main__":
    run(mode="toolbox")
