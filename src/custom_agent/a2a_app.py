"""Native Agent-to-Agent (A2A) server for the custom weather agent.

Self-contained copy (no shared modules). Builds the A2A routes (agent card +
JSON-RPC) using the ``a2a-sdk`` v1.0+ API — the ``A2AStarletteApplication``
wrapper class was removed in the 0.3 → 1.0 rewrite in favor of plain Starlette
route-factory functions (``a2a.server.routes.create_agent_card_routes`` /
``create_jsonrpc_routes``) that get merged directly into the host app's route
list. See the SDK's v0.3 → v1.0 migration guide for background.

``enable_v0_3_compat=True`` is passed to ``create_jsonrpc_routes`` so the
existing v0.3-shaped ``message/send`` payload used by
``src/clients/protocols.py``'s ``A2aClient`` keeps working unchanged against
the v1.0+ server.

``build_a2a_routes`` returns ``None`` (with a warning) if the ``a2a`` SDK is
not installed, so a container can still serve the other protocols.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_agent_card(public_url: str, *, streaming: bool = True) -> Any:
    """Build the A2A ``AgentCard`` advertised at ``/.well-known/agent-card.json``."""
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

    skill = AgentSkill(
        id="weather",
        name="Weather",
        description="Report the current weather and forecast for a city.",
        tags=["weather", "forecast"],
        examples=["What is the weather in Berlin?", "Give me a 3 day forecast for Tokyo."],
    )
    return AgentCard(
        name="weather-agent",
        description="Reports current weather and forecast for a set of cities via an MCP tool.",
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", protocol_version="0.3", url=public_url),
        ],
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=streaming),
        skills=[skill],
    )


def build_a2a_routes(runner: Any, public_url: str) -> list[Any] | None:
    """Return the A2A Starlette routes for ``runner``, or ``None`` if a2a is absent."""
    try:
        from a2a.helpers import new_text_message
        from a2a.server.agent_execution import AgentExecutor, RequestContext
        from a2a.server.events import EventQueue
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
        from a2a.server.tasks import InMemoryTaskStore
        from a2a.types import Role
    except Exception:  # noqa: BLE001 - a2a optional; skip the routes if missing
        logger.warning("a2a SDK not installed — A2A endpoint disabled", exc_info=True)
        return None

    class WeatherAgentExecutor(AgentExecutor):
        """Bridge A2A requests to the weather runner."""

        def __init__(self, agent_runner: Any) -> None:
            self._runner = agent_runner

        async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
            query = context.get_user_input()
            session_id = getattr(context, "context_id", None)
            answer = await self._runner.run(query, session_id=session_id)
            await event_queue.enqueue_event(new_text_message(answer, role=Role.ROLE_AGENT))

        async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
            raise NotImplementedError("Cancellation is not supported by the weather agent.")

    agent_card = build_agent_card(public_url)
    handler = DefaultRequestHandler(
        agent_executor=WeatherAgentExecutor(runner),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    routes: list[Any] = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(handler, rpc_url="/a2a", enable_v0_3_compat=True))
    return routes
