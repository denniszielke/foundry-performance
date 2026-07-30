"""Custom MAF weather agent — Azure Container App, outside Foundry.

Microsoft Agent Framework agent whose weather tool uses direct MCP by default
and can be routed through Foundry toolbox with ``WEATHER_TOOL_MODE=toolbox``.
The multi-protocol host serves this one native agent over Responses,
Invocations, A2A, and WebSocket routes.

Run locally from the project root::

    WEATHER_MCP_URL=http://127.0.0.1:8093/mcp python -m src.custom_agent_maf.agent
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

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


def _tool_mode() -> str:
    mode = os.getenv("WEATHER_TOOL_MODE", "direct").strip().lower()
    if mode not in {"direct", "toolbox"}:
        raise RuntimeError("WEATHER_TOOL_MODE must be 'direct' or 'toolbox'.")
    return mode


def _toolbox_url() -> str:
    endpoint = os.getenv("FOUNDRY_TOOLBOX_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    toolbox = os.getenv("WEATHER_TOOLBOX_NAME", "weather-tools")
    return f"{_project_endpoint()}/toolboxes/{toolbox}/mcp?api-version=v1"


class _ToolboxAuth(httpx.Auth):
    def __init__(self, token_provider: Callable[[], str]) -> None:
        self._token_provider = token_provider

    def auth_flow(self, request: httpx.Request):  # noqa: ANN201
        request.headers["Authorization"] = f"Bearer {self._token_provider()}"
        yield request


def build_agent() -> Any:
    """Build the native Agent Framework agent and selected MCP weather tool."""
    from agent_framework import Agent, MCPStreamableHTTPTool
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    credential = DefaultAzureCredential()
    tool_mode = _tool_mode()
    tool_url = (
        _toolbox_url()
        if tool_mode == "toolbox"
        else os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8093/mcp").strip()
    )
    tool_kwargs: dict[str, Any] = {}
    if tool_mode == "toolbox":
        token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
        tool_kwargs["http_client"] = httpx.AsyncClient(
            auth=_ToolboxAuth(token_provider),
            headers={"Foundry-Features": "Toolboxes=V1Preview"},
            timeout=120.0,
        )
    logger.info("Weather tool mode=%s url=%s", tool_mode, tool_url)
    mcp_tool = MCPStreamableHTTPTool(name="weather", url=tool_url, load_prompts=False, **tool_kwargs)

    model = (
        os.getenv("FOUNDRY_MODEL")
        or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME")
        or "gpt-4.1-mini"
    )
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
