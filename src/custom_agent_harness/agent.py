"""Interactive Agent Framework harness for the weather MCP server.

Run locally from the project root::

    WEATHER_MCP_URL=http://127.0.0.1:8093/mcp python -m src.custom_agent_harness.agent
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from dotenv import load_dotenv

INSTRUCTIONS = (
    "You are a weather assistant. Plan each multi-step request with the todo tools, "
    "then execute the approved plan. Use the weather MCP tools for current weather "
    "and forecasts. Call list_cities when you are unsure which cities are available, "
    "and never guess weather data."
)


def _project_endpoint() -> str:
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("Set AZURE_AI_PROJECT_ENDPOINT (or FOUNDRY_PROJECT_ENDPOINT).")
    return endpoint.rstrip("/")


def build_agent() -> Any:
    """Build the plan-and-execute harness with a direct weather MCP tool."""
    from agent_framework import (
        MCPStreamableHTTPTool,
        create_harness_agent,
        todos_remaining,
        todos_remaining_message,
    )
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import AzureCliCredential

    weather_mcp_url = os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8093/mcp").strip()
    if not weather_mcp_url:
        raise RuntimeError("Set WEATHER_MCP_URL to the direct weather MCP endpoint.")
    weather_tool = MCPStreamableHTTPTool(
        name="weather",
        url=weather_mcp_url,
        load_prompts=False,
    )

    model = (
        os.getenv("FOUNDRY_MODEL")
        or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        or "gpt-4.1-mini"
    )
    client = FoundryChatClient(
        project_endpoint=_project_endpoint(),
        model=model,
        credential=AzureCliCredential(),
    )

    return create_harness_agent(
        client=client,
        max_context_window_tokens=128_000,
        max_output_tokens=16_384,
        name="WeatherHarness",
        description="Plans and executes weather requests using a direct MCP server.",
        agent_instructions=INSTRUCTIONS,
        tools=weather_tool,
        disable_web_search=True,
        loop_should_continue=todos_remaining(looping_modes=["execute"]),
        loop_next_message=todos_remaining_message,
        loop_max_iterations=10,
    )


async def main() -> None:
    """Run the harness in its interactive terminal UI."""
    from .console import run_agent_async

    load_dotenv()
    agent = build_agent()
    await run_agent_async(agent, session=agent.create_session(), initial_mode="plan")


if __name__ == "__main__":
    asyncio.run(main())
