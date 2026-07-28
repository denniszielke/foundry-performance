"""Hosted weather agent — invocations protocol only (Foundry-hosted container).

Single-protocol variation: this container implements *only* the invocations
protocol — both the plain ``POST /invocations`` request/response route and
the duplex ``/invocations_ws`` WebSocket route (the same host serves both;
wiring one handler for each is not "multi-protocol composition", it's this
one host's two entry points). No other protocol host is composed in.

``POST /invocations`` is wired by the **built-in**
:class:`agent_framework_foundry_hosting.InvocationsHostServer` instead of a
hand-rolled handler — it already implements the ``{"message": ...}`` in /
``{"response": ...}`` out JSON contract, per-session ``AgentSession``
bookkeeping, and SSE streaming, so there's no reason to reimplement it. Only
``/invocations_ws`` (a transport that built-in host doesn't cover) gets a
thin ``ws_handler``.

The weather tool is reached through the **Foundry toolbox** (MCP over HTTP,
authenticated with the agent's managed identity). The MCP connection + agent
are built lazily on the first request, which is exactly the cold start the
benchmark measures — that laziness lives in ``LazyWeatherAgent``, a small
``SupportsAgentRun`` shim handed straight to ``InvocationsHostServer``.

Run locally from the project root::

    python -m src.hosted_agent_invocations.agent
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, AsyncGenerator, Callable

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a helpful weather assistant. Answer questions about the current "
    "weather and forecast for a city using the available weather tools. Call "
    "list_cities if you are unsure which cities are available. Always call a "
    "tool for real data instead of guessing, and keep answers short."
)

# Bounded retry/backoff for the initial MCP toolbox connection. The toolbox
# authorizes callers via the AI account's system-assigned identity (granted by
# ``scripts.ensure_toolbox_role`` right before this agent is deployed); that
# Entra role assignment can take a few minutes to propagate, so the very first
# live request(s) against a freshly deployed container can see a transient 401
# even though the role assignment already exists. Retrying only costs time on
# that failure path — it does not add latency to a normal (already-propagated)
# cold start.
_MCP_CONNECT_ATTEMPTS = 3
_MCP_CONNECT_INITIAL_DELAY = 2.0


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


def _bearer_header_provider(scope: str = "https://ai.azure.com/.default") -> Callable[[dict[str, Any]], dict[str, str]]:
    """Inject a fresh Entra bearer token per MCP request (agent managed identity)."""
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    get_token = get_bearer_token_provider(DefaultAzureCredential(), scope)

    def provide(_kwargs: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": "Bearer " + get_token()}

    return provide


def _configure_telemetry() -> None:
    """Best-effort Application Insights wiring (Foundry injects the connection string)."""
    conn = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not conn:
        logger.info("APPLICATIONINSIGHTS_CONNECTION_STRING not set — tracing disabled")
        return
    try:
        from agent_framework.observability import setup_observability

        setup_observability(applicationinsights_connection_string=conn)
        logging.getLogger("azure").setLevel(logging.WARNING)
        logger.info("agent-framework observability configured (Application Insights)")
    except Exception:  # noqa: BLE001 - tracing must never break the agent
        logger.warning("Could not configure Application Insights instrumentation", exc_info=True)


class LazyWeatherAgent:
    """A ``SupportsAgentRun`` shim that builds the real MAF agent on first use.

    Structurally satisfies ``agent_framework.SupportsAgentRun`` (duck-typed —
    ``run``/``create_session``/``get_session`` plus ``id``/``name``/
    ``description``) so it can be handed straight to the built-in
    ``InvocationsHostServer``. The MCP toolbox connection and the underlying
    ``Agent`` are only constructed inside ``run``, on the first call — that is
    the cold start the benchmark measures.
    """

    id = "weather-agent"
    name = "weather-agent"
    description = "Weather assistant backed by the Foundry weather toolbox."

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._agent: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_agent(self) -> Any:
        """Open the MCP connection and construct the agent (idempotent)."""
        if self._agent is not None:
            return self._agent
        async with self._lock:
            if self._agent is not None:
                return self._agent

            from agent_framework import Agent, MCPStreamableHTTPTool
            from agent_framework.foundry import FoundryChatClient
            from azure.identity import DefaultAzureCredential

            url = _toolbox_url()
            logger.info("Weather agent (toolbox) tool url=%s", url)
            mcp_tool = await self._connect_mcp_tool(MCPStreamableHTTPTool, url)

            model = os.getenv("FOUNDRY_MODEL") or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME") or "gpt-4.1-mini"
            client = FoundryChatClient(
                project_endpoint=_project_endpoint(),
                model=model,
                credential=DefaultAzureCredential(),
            )
            self._agent = await self._stack.enter_async_context(
                Agent(client=client, name="weather-agent", instructions=INSTRUCTIONS, tools=mcp_tool)
            )
            return self._agent

    async def _connect_mcp_tool(self, tool_cls: Any, url: str) -> Any:
        """Enter the MCP toolbox tool's async context, retrying transient failures.

        A failed ``__aenter__`` never pushes anything onto ``self._stack``, so
        it is safe to retry against the same stack.
        """
        delay = _MCP_CONNECT_INITIAL_DELAY
        for attempt in range(1, _MCP_CONNECT_ATTEMPTS + 1):
            try:
                return await self._stack.enter_async_context(
                    tool_cls(name="weather", url=url, load_prompts=False, header_provider=_bearer_header_provider())
                )
            except Exception:
                if attempt == _MCP_CONNECT_ATTEMPTS:
                    raise
                logger.warning(
                    "MCP toolbox connect attempt %d/%d failed (likely RBAC propagation lag); retrying in %.0fs",
                    attempt,
                    _MCP_CONNECT_ATTEMPTS,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    def run(self, messages: Any = None, *, stream: bool = False, session: Any = None, **kwargs: Any) -> Any:
        """Match ``SupportsAgentRun.run``: an awaitable when ``stream=False``,
        an async generator (no ``await`` needed to start iterating) when
        ``stream=True`` — both cases lazily build the agent first.
        """
        if stream:
            return self._run_stream(messages, session=session, **kwargs)
        return self._run_blocking(messages, session=session, **kwargs)

    async def _run_blocking(self, messages: Any, *, session: Any = None, **kwargs: Any) -> Any:
        agent = await self._ensure_agent()
        return await agent.run(messages, session=session, **kwargs)

    async def _run_stream(self, messages: Any, *, session: Any = None, **kwargs: Any) -> AsyncGenerator[Any, None]:
        agent = await self._ensure_agent()
        async for update in agent.run(messages, stream=True, session=session, **kwargs):
            yield update

    def create_session(self, *, session_id: str | None = None) -> Any:
        from agent_framework import AgentSession

        return AgentSession(session_id=session_id)

    def get_session(self, service_session_id: Any, *, session_id: str | None = None) -> Any:
        from agent_framework import AgentSession

        return AgentSession(service_session_id=service_session_id, session_id=session_id)

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._agent = None


weather_agent = LazyWeatherAgent()


def build_host() -> Any:
    """Wire the built-in ``InvocationsHostServer`` for ``POST /invocations``
    and add a thin ``ws_handler`` for the ``/invocations_ws`` duplex route
    (the built-in host only covers the plain request/response transport).
    """
    from agent_framework import AgentSession
    from agent_framework_foundry_hosting import InvocationsHostServer
    from starlette.websockets import WebSocket, WebSocketDisconnect

    app = InvocationsHostServer(agent=weather_agent)

    ws_sessions: dict[str, AgentSession] = {}

    def _ws_session(session_id: str | None) -> AgentSession | None:
        if not session_id:
            return None
        session = ws_sessions.get(session_id)
        if session is None:
            session = AgentSession(session_id=session_id)
            ws_sessions[session_id] = session
        return session

    @app.ws_handler
    async def _ws(websocket: WebSocket) -> None:
        session = _ws_session(websocket.query_params.get("agent_session_id"))
        try:
            async for raw in websocket.iter_text():
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "message": "invalid JSON frame"}))
                    continue
                if evt.get("type") != "message":
                    continue
                full: list[str] = []
                try:
                    async for update in weather_agent.run(evt.get("text", "") or "", stream=True, session=session):
                        delta = getattr(update, "text", None)
                        if delta:
                            full.append(delta)
                            await websocket.send_text(json.dumps({"type": "delta", "text": delta}))
                    await websocket.send_text(json.dumps({"type": "done", "text": "".join(full)}))
                except Exception as exc:  # noqa: BLE001 - report per-turn failures
                    logger.exception("weather agent ws turn failed")
                    await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        except WebSocketDisconnect:
            pass

    @app.shutdown_handler
    async def _on_shutdown() -> None:
        await weather_agent.aclose()

    return app


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting hosted weather agent (invocations) — image_tag=%s", os.getenv("IMAGE_TAG", "unknown"))
    _configure_telemetry()
    build_host().run()
