"""Foundry-hosted weather agent using AG-UI over the invocations protocol."""

from __future__ import annotations

import logging
import os
from typing import Callable
from urllib.parse import urlparse

import httpx
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI, AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.ag_ui import handle_ag_ui_request
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from starlette.requests import Request
from starlette.responses import Response

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

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"


def _endpoint_type() -> str:
    endpoint_type = os.getenv("AZURE_AI_ENDPOINT_TYPE", "foundry").strip().lower()
    if endpoint_type not in {"foundry", "openai"}:
        raise EnvironmentError("AZURE_AI_ENDPOINT_TYPE must be 'foundry' or 'openai'.")
    return endpoint_type


def _project_endpoint() -> str:
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise EnvironmentError("Set AZURE_AI_PROJECT_ENDPOINT (or FOUNDRY_PROJECT_ENDPOINT).")
    return endpoint.rstrip("/")


def _openai_endpoint() -> str:
    parsed = urlparse(_project_endpoint())
    suffix = ".services.ai.azure.com"
    if not parsed.hostname or not parsed.hostname.endswith(suffix):
        raise EnvironmentError("AZURE_AI_PROJECT_ENDPOINT must use a *.services.ai.azure.com host.")
    resource = parsed.hostname[: -len(suffix)]
    return f"{parsed.scheme}://{resource}.openai.azure.com"


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


model_deployment = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.getenv("FOUNDRY_MODEL") or "gpt-4.1-mini"
credential = DefaultAzureCredential()
toolbox_token_provider = get_bearer_token_provider(credential, FOUNDRY_SCOPE)

if _endpoint_type() == "openai":
    model_token_provider = get_bearer_token_provider(credential, AZURE_OPENAI_SCOPE)
    openai_client = AsyncAzureOpenAI(
        azure_endpoint=_openai_endpoint(),
        azure_deployment=model_deployment,
        azure_ad_token_provider=model_token_provider,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "preview"),
    )
else:
    model_token_provider = get_bearer_token_provider(credential, FOUNDRY_SCOPE)
    openai_client = AsyncOpenAI(
        base_url=f"{_project_endpoint()}/openai/v1/",
        api_key=model_token_provider,
    )
model = OpenAIResponsesModel(model_deployment, provider=OpenAIProvider(openai_client=openai_client))

toolbox_http_client = httpx.AsyncClient(
    auth=_ToolboxAuth(toolbox_token_provider),
    headers={"Foundry-Features": "Toolboxes=V1Preview"},
    timeout=120.0,
)
weather_toolbox = MCPServerStreamableHTTP(
    _toolbox_endpoint(),
    http_client=toolbox_http_client,
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