"""Custom MAF weather agent — Azure Container App, outside Foundry.

Microsoft Agent Framework agent whose weather tool connects **directly to the
MCP server** (no toolbox, no auth). The multi-protocol host serves this one
native agent over Responses, Invocations, A2A, and WebSocket routes.

Run locally from the project root::

    WEATHER_MCP_URL=http://127.0.0.1:8093/mcp python -m src.custom_agent_maf.agent
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a helpful weather assistant. Answer questions about the current "
    "weather and forecast for a city using the available weather tools. Call "
    "list_cities if you are unsure which cities are available. Always call a "
    "tool for real data instead of guessing. Format current-weather answers exactly as: "
    "{\"city\":\"<city>\"}In **<city>, <country>**, it's currently **<temperature>°C** "
    "(feels like **<feels_like>°C**) with **<condition>**.\n"
    "**Humidity:** <humidity>% • **Wind:** <wind> kph • **Precipitation:** <precipitation> mm. "
    "Format forecast answers as: {\"city\":\"<city>\",\"days\":<days>}"
    "**<city>, <country> — next <days> days:** followed by exactly one line per day: "
    "- **<date>:** <condition>, **<temperature>°C** (feels like **<feels_like>°C**), "
    "humidity **<humidity>%**, wind **<wind> kph**, precipitation **<precipitation> mm**. "
    "Do not add an introduction, conclusion, raw tool output, or other commentary."
)


def _project_endpoint() -> str:
    """Foundry project endpoint (bicep output ``AZURE_AI_PROJECT_ENDPOINT``)."""
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("Set AZURE_AI_PROJECT_ENDPOINT (or FOUNDRY_PROJECT_ENDPOINT).")
    return endpoint.rstrip("/")


def _endpoint_type() -> str:
    endpoint_type = os.getenv("AZURE_AI_ENDPOINT_TYPE", "foundry").strip().lower()
    if endpoint_type not in {"foundry", "openai"}:
        raise RuntimeError("AZURE_AI_ENDPOINT_TYPE must be 'foundry' or 'openai'.")
    return endpoint_type


def _openai_endpoint() -> str:
    parsed = urlparse(_project_endpoint())
    suffix = ".services.ai.azure.com"
    if not parsed.hostname or not parsed.hostname.endswith(suffix):
        raise RuntimeError("AZURE_AI_PROJECT_ENDPOINT must use a *.services.ai.azure.com host.")
    resource = parsed.hostname[: -len(suffix)]
    return f"{parsed.scheme}://{resource}.openai.azure.com"


def build_agent() -> Any:
    """Build the native Agent Framework agent and direct MCP weather tool."""
    from agent_framework import Agent, MCPStreamableHTTPTool
    from azure.identity import DefaultAzureCredential

    tool_url = os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8093/mcp").strip()
    logger.info("Weather agent (direct) tool url=%s", tool_url)
    mcp_tool = MCPStreamableHTTPTool(name="weather", url=tool_url, load_prompts=False)

    model = (
        os.getenv("FOUNDRY_MODEL")
        or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME")
        or "gpt-4.1-mini"
    )
    credential = DefaultAzureCredential()
    if _endpoint_type() == "openai":
        from agent_framework.openai import OpenAIChatClient

        client = OpenAIChatClient(
            model=model,
            azure_endpoint=_openai_endpoint(),
            credential=credential,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "preview"),
        )
    else:
        from agent_framework.foundry import FoundryChatClient

        client = FoundryChatClient(
            project_endpoint=_project_endpoint(),
            model=model,
            credential=credential,
        )
    return Agent(
        client=client,
        id=os.getenv("OTEL_AGENT_ID") or None,
        name="weather-agent-maf",
        instructions=INSTRUCTIONS,
        tools=mcp_tool,
    )


if __name__ == "__main__":
    from src.custom_agent_maf.multiprotocol import run

    run()
