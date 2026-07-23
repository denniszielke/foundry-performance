"""Shared weather-agent runner built on the Microsoft Agent Framework.

One runner powers every agent variation and every protocol. It wires a
``FoundryChatClient`` agent to the weather MCP server through MAF's
``MCPStreamableHTTPTool`` and exposes a tiny surface the protocol handlers use:

* :meth:`run` — full answer (non-streaming).
* :meth:`run_stream` — async iterator of text deltas (for TTFB measurement).

The **only** difference between the two container variations is where the MCP
tool points and whether an auth header is attached:

* ``direct``  — straight at the weather MCP server ``/mcp`` (no auth).
* ``toolbox`` — at the Foundry toolbox MCP endpoint (agent managed-identity token).

Sessions map to Agent Framework *threads* so a client can measure a first
request vs. a follow-up request that reuses conversation state.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from typing import Any

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a helpful weather assistant. Answer questions about the current "
    "weather and forecast for a city using the available weather tools. Call "
    "list_cities if you are unsure which cities are available. Always call a "
    "tool for real data instead of guessing, and keep answers short."
)


def _bearer_header_provider(scope: str = "https://ai.azure.com/.default") -> Callable[[dict[str, Any]], dict[str, str]]:
    """Header provider that injects a fresh Entra bearer token per MCP request.

    Used for the Foundry toolbox endpoint, which authenticates callers with the
    agent's managed identity. The weather MCP server itself is anonymous.
    """
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    get_token = get_bearer_token_provider(DefaultAzureCredential(), scope)

    def provide(_kwargs: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": "Bearer " + get_token()}

    return provide


def _project_endpoint() -> str:
    """Foundry project endpoint (bicep output ``AZURE_AI_PROJECT_ENDPOINT``)."""
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("Set AZURE_AI_PROJECT_ENDPOINT (or FOUNDRY_PROJECT_ENDPOINT).")
    return endpoint.rstrip("/")


def _resolve_tool_url(mode: str) -> tuple[str, Callable[[dict[str, Any]], dict[str, str]] | None]:
    """Return ``(mcp_url, header_provider)`` for the requested tool mode."""
    if mode == "toolbox":
        url = os.getenv("FOUNDRY_TOOLBOX_ENDPOINT", "").strip()
        if not url:
            toolbox = os.getenv("WEATHER_TOOLBOX_NAME", "weather-tools")
            url = f"{_project_endpoint()}/toolboxes/{toolbox}/mcp?api-version=v1"
        return url, _bearer_header_provider()

    # direct mode — straight at the weather MCP server, no auth.
    url = os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8093/mcp").strip()
    return url, None


class WeatherAgentRunner:
    """Lifecycle wrapper around a MAF agent + MCP weather tool."""

    def __init__(
        self,
        *,
        mode: str | None = None,
        instructions: str = INSTRUCTIONS,
        agent_name: str = "weather-agent",
    ) -> None:
        self._mode = (mode or os.getenv("WEATHER_TOOL_MODE", "direct")).strip().lower()
        self._instructions = instructions
        self._agent_name = agent_name
        self._stack = AsyncExitStack()
        self._agent: Any = None
        self._threads: dict[str, Any] = {}
        self._started = False

    @property
    def mode(self) -> str:
        return self._mode

    async def start(self) -> None:
        """Open the MCP connection and construct the agent (idempotent)."""
        if self._started:
            return

        from agent_framework import Agent, MCPStreamableHTTPTool
        from agent_framework.foundry import FoundryChatClient
        from azure.identity import DefaultAzureCredential

        url, header_provider = _resolve_tool_url(self._mode)
        logger.info("Weather agent tool mode=%s url=%s", self._mode, url)

        tool_kwargs: dict[str, Any] = {"name": "weather", "url": url, "load_prompts": False}
        if header_provider is not None:
            tool_kwargs["header_provider"] = header_provider
        mcp_tool = await self._stack.enter_async_context(MCPStreamableHTTPTool(**tool_kwargs))

        model = os.getenv("FOUNDRY_MODEL") or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME") or "gpt-4.1-mini"
        client = FoundryChatClient(
            project_endpoint=_project_endpoint(),
            model=model,
            credential=DefaultAzureCredential(),
        )
        self._agent = await self._stack.enter_async_context(
            Agent(client=client, name=self._agent_name, instructions=self._instructions, tools=mcp_tool)
        )
        self._started = True

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._started = False

    async def __aenter__(self) -> "WeatherAgentRunner":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _thread(self, session_id: str | None) -> Any:
        """Get (or lazily create) the conversation thread for a session id."""
        if not session_id:
            return self._agent.get_new_thread()
        thread = self._threads.get(session_id)
        if thread is None:
            thread = self._agent.get_new_thread()
            self._threads[session_id] = thread
        return thread

    async def run(self, text: str, session_id: str | None = None) -> str:
        if not self._started:
            await self.start()
        result = await self._agent.run(text, thread=self._thread(session_id))
        return result.text or ""

    async def run_stream(self, text: str, session_id: str | None = None) -> AsyncIterator[str]:
        if not self._started:
            await self.start()
        thread = self._thread(session_id)
        async for update in self._agent.run_stream(text, thread=thread):
            delta = getattr(update, "text", None)
            if delta:
                yield delta
