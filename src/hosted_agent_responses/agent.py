"""Hosted weather agent — responses protocol only (Foundry-hosted container).

Single-protocol variation: this container implements *only* the OpenAI
Responses protocol (``POST /responses``, streaming SSE), served by the
built-in ``agent_framework_foundry_hosting.ResponsesHostServer``. There is no
custom response handler here — the host owns request parsing, conversation
history/threading, streaming and lazily entering the agent's (and its MCP
tool's) async context on first use.

A2A is **not** served in-container — Foundry hosted agents that implement the
responses protocol get native incoming A2A support fronted by the platform
itself (enabled on the endpoint by ``scripts.deploy_hosted_agent_responses``).
See:
https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint

The weather tool is reached through the **Foundry toolbox** (MCP over HTTP,
authenticated with the agent's managed identity). ``ResponsesHostServer``
enters the agent's async context (which opens the MCP connection) lazily on
the first request, which is exactly the cold start the benchmark measures.

Run locally from the project root::

    python -m src.hosted_agent_responses.agent
"""

from __future__ import annotations

import logging
import os
from typing import Callable

import httpx
from agent_framework import MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a helpful weather assistant. Answer questions about the current "
    "weather and forecast for a city using the available weather tools. Call "
    "list_cities if you are unsure which cities are available. Always call a "
    "tool for real data instead of guessing, and keep answers short."
)


def _project_endpoint() -> str:
    """Foundry project endpoint (bicep output ``AZURE_AI_PROJECT_ENDPOINT``)."""
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("Set AZURE_AI_PROJECT_ENDPOINT (or FOUNDRY_PROJECT_ENDPOINT).")
    return endpoint.rstrip("/")


def _toolbox_url() -> str:
    """Foundry toolbox MCP endpoint for the weather tools."""
    url = os.getenv("FOUNDRY_TOOLBOX_ENDPOINT", "").strip()
    if not url:
        toolbox = os.getenv("WEATHER_TOOLBOX_NAME", "weather-tools")
        url = f"{_project_endpoint()}/toolboxes/{toolbox}/mcp?api-version=v1"
    return url


class _ToolboxAuth(httpx.Auth):
    """Injects a fresh Entra bearer token on every request to the toolbox."""

    def __init__(self, token_provider: Callable[[], str]) -> None:
        self._get_token = token_provider

    def auth_flow(self, request: httpx.Request):  # noqa: ANN201
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request


_credential = DefaultAzureCredential()
_token_provider = get_bearer_token_provider(_credential, "https://ai.azure.com/.default")

# The MCP tool and agent are plain module-level objects — ``ResponsesHostServer``
# lazily enters the agent's (and therefore the MCP tool's) async context on the
# first request, so opening the toolbox connection is still deferred to the
# first live request rather than happening at import time.
_http_client = httpx.AsyncClient(
    auth=_ToolboxAuth(_token_provider),
    headers={"Foundry-Features": "Toolboxes=V1Preview"},
    timeout=120.0,
)

_mcp_tool = MCPStreamableHTTPTool(
    name="weather",
    url=_toolbox_url(),
    http_client=_http_client,
    load_prompts=False,
)

_chat_client = FoundryChatClient(
    project_endpoint=_project_endpoint(),
    model=os.getenv("FOUNDRY_MODEL") or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME") or "gpt-4.1-mini",
    credential=_credential,
)

agent = _chat_client.as_agent(
    name="weather-agent",
    instructions=INSTRUCTIONS,
    tools=_mcp_tool,
)


if __name__ == "__main__":
    # Application Insights tracing is configured automatically by
    # azure-ai-agentserver-core from APPLICATIONINSIGHTS_CONNECTION_STRING
    # (Foundry injects it) — no manual observability setup needed here.
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting hosted weather agent (responses) — image_tag=%s", os.getenv("IMAGE_TAG", "unknown"))
    ResponsesHostServer(agent).run()
