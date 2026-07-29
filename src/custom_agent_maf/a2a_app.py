"""Native Agent Framework A2A server for the custom MAF weather agent.

Builds the A2A routes (agent card + JSON-RPC) with Agent Framework's
``A2AExecutor`` and the official ``a2a-sdk`` route factories.

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
        name="weather-agent-maf",
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


def build_a2a_routes(agent: Any, public_url: str) -> list[Any]:
    """Return native Agent Framework A2A routes for ``agent``."""
    try:
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
        from a2a.server.tasks import InMemoryTaskStore
        from agent_framework.a2a import A2AExecutor
    except ImportError as exc:
        raise RuntimeError(
            "A2A hosting requires agent-framework-a2a and a2a-sdk[http-server]."
        ) from exc

    agent_card = build_agent_card(public_url)
    handler = DefaultRequestHandler(
        agent_executor=A2AExecutor(agent, stream=True),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    routes: list[Any] = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(handler, rpc_url="/a2a", enable_v0_3_compat=True))
    return routes
