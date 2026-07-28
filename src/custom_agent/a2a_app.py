"""Native Agent-to-Agent (A2A) server for the custom weather agent.

Self-contained copy (no shared modules). Builds a mountable Starlette app using
the native ``a2a`` SDK executor model (``A2AStarletteApplication`` +
``AgentExecutor``), backed by :class:`.runner.WeatherAgentRunner`.

``build_a2a_app`` returns ``None`` (with a warning) if the ``a2a`` SDK is not
installed, so a container can still serve the other protocols.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_agent_card(public_url: str, *, streaming: bool = True) -> Any:
    """Build the A2A ``AgentCard`` advertised at ``/.well-known/agent-card.json``."""
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill

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
        url=public_url,
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=streaming),
        skills=[skill],
    )


def build_a2a_app(runner: Any, public_url: str) -> Any | None:
    """Return a built Starlette A2A app wrapping ``runner``, or ``None`` if a2a is absent."""
    try:
        from a2a.server.agent_execution import AgentExecutor, RequestContext
        from a2a.server.apps import A2AStarletteApplication
        from a2a.server.events import EventQueue
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.tasks import InMemoryTaskStore
        from a2a.utils import new_agent_text_message
    except Exception:  # noqa: BLE001 - a2a optional; skip the mount if missing
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
            await event_queue.enqueue_event(new_agent_text_message(answer))

        async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
            raise NotImplementedError("Cancellation is not supported by the weather agent.")

    handler = DefaultRequestHandler(
        agent_executor=WeatherAgentExecutor(runner),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(agent_card=build_agent_card(public_url), http_handler=handler).build()
