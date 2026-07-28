"""Foundry-hosted weather agent using AG-UI over the invocations protocol."""

from __future__ import annotations

import logging
import os
from typing import Callable
from urllib.parse import urlparse

import httpx
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI
from pydantic_ai import Agent
from pydantic_ai.ag_ui import handle_ag_ui_request
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a helpful weather assistant. Answer questions about the current "
    "weather and forecast for a city using the available weather tools. Call "
    "list_cities if you are unsure which cities are available. Always call a "
    "tool for real data instead of guessing, and keep answers short."
)


def _project_endpoint() -> str:
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise EnvironmentError("Set AZURE_AI_PROJECT_ENDPOINT (or FOUNDRY_PROJECT_ENDPOINT).")
    return endpoint.rstrip("/")


def _toolbox_endpoint() -> str:
    endpoint = os.getenv("FOUNDRY_TOOLBOX_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    toolbox_name = os.getenv("WEATHER_TOOLBOX_NAME", "weather-tools")
    return f"{_project_endpoint()}/toolboxes/{toolbox_name}/mcp?api-version=v1"


class _ToolboxAuth(httpx.Auth):
    """Adds a fresh managed-identity token to each Toolbox MCP request."""

    def __init__(self, token_provider: Callable[[], str]) -> None:
        self._token_provider = token_provider

    def auth_flow(self, request: httpx.Request):  # noqa: ANN201
        request.headers["Authorization"] = f"Bearer {self._token_provider()}"
        yield request


project_endpoint = _project_endpoint()
model_deployment = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.getenv("FOUNDRY_MODEL") or "gpt-4.1-mini"
credential = DefaultAzureCredential()
token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")

parsed_endpoint = urlparse(project_endpoint)
openai_client = AsyncAzureOpenAI(
    azure_endpoint=f"{parsed_endpoint.scheme}://{parsed_endpoint.netloc}",
    azure_deployment=model_deployment,
    azure_ad_token_provider=token_provider,
    api_version="2025-04-01-preview",
)
model = OpenAIResponsesModel(model_deployment, provider=OpenAIProvider(openai_client=openai_client))

weather_toolbox = MCPToolset(
    _toolbox_endpoint(),
    auth=_ToolboxAuth(token_provider),
    headers={"Foundry-Features": "Toolboxes=V1Preview"},
    include_instructions=True,
)
agent = Agent(model, instructions=INSTRUCTIONS, toolsets=[weather_toolbox])

app = InvocationAgentServerHost()


@app.invoke_handler
async def handle_invoke(request: Request) -> Response:
    return await handle_ag_ui_request(agent, request)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    app.run()