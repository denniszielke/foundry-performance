"""Custom weather agent (variation 3) — Azure Container App, outside Foundry.

Microsoft Agent Framework agent whose weather tool connects **directly to the
MCP server** (no toolbox, no auth). It serves every protocol itself from a plain
container using the native agent-framework / a2a hosting and the
``azure-ai-agentserver`` protocol hosts.

All of this agent's own logic lives in this file, including its
:class:`WeatherAgentRunner` (the MAF agent + MCP weather tool lifecycle):

* :meth:`WeatherAgentRunner.run` — full answer (non-streaming).
* :meth:`WeatherAgentRunner.run_stream` — async iterator of text deltas (TTFB).

Sessions map to Agent Framework *threads* so a client can measure a first
request vs. a follow-up request that reuses conversation state.

Run locally from the project root::

    WEATHER_MCP_URL=http://127.0.0.1:8093/mcp python -m src.custom_agent.agent
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

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


class WeatherAgentRunner:
    """Lifecycle wrapper around a MAF agent + direct (anonymous) MCP weather tool."""

    def __init__(self) -> None:
        # A stable agent id is stamped as ``gen_ai.agent.id`` on every span so a
        # Foundry external-agent registration can match this agent's traces.
        self._agent_id = os.getenv("OTEL_AGENT_ID") or None
        self._stack = AsyncExitStack()
        self._agent: Any = None
        self._sessions: dict[str, Any] = {}
        self._started = False

    async def start(self) -> None:
        """Open the MCP connection and construct the agent (idempotent)."""
        if self._started:
            return

        from agent_framework import Agent, MCPStreamableHTTPTool
        from agent_framework.foundry import FoundryChatClient
        from azure.identity import DefaultAzureCredential

        url = os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8093/mcp").strip()
        logger.info("Weather agent (direct) tool url=%s", url)
        mcp_tool = await self._stack.enter_async_context(
            MCPStreamableHTTPTool(name="weather", url=url, load_prompts=False)
        )

        model = os.getenv("FOUNDRY_MODEL") or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME") or "gpt-4.1-mini"
        client = FoundryChatClient(
            project_endpoint=_project_endpoint(),
            model=model,
            credential=DefaultAzureCredential(),
        )
        self._agent = await self._stack.enter_async_context(
            Agent(client=client, id=self._agent_id, name="weather-agent", instructions=INSTRUCTIONS, tools=mcp_tool)
        )
        self._started = True

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._started = False

    def _session(self, session_id: str | None) -> Any:
        """Get (or lazily create) the conversation session for a session id."""
        if not session_id:
            return self._agent.create_session()
        session = self._sessions.get(session_id)
        if session is None:
            session = self._agent.create_session(session_id=session_id)
            self._sessions[session_id] = session
        return session

    async def run(self, text: str, session_id: str | None = None) -> str:
        if not self._started:
            await self.start()
        result = await self._agent.run(text, session=self._session(session_id))
        return result.text or ""

    async def run_stream(self, text: str, session_id: str | None = None) -> AsyncIterator[str]:
        if not self._started:
            await self.start()
        session = self._session(session_id)
        async for update in self._agent.run(text, stream=True, session=session):
            delta = getattr(update, "text", None)
            if delta:
                yield delta


if __name__ == "__main__":
    from src.custom_agent.multiprotocol import run

    run()
