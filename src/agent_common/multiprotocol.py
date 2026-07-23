"""Multi-protocol host that exposes one weather agent over every protocol.

A single :class:`~src.agent_common.runner.WeatherAgentRunner` is served over:

* **responses**       — ``POST /responses`` (OpenAI Responses, streaming SSE)
* **invocations**     — ``POST /invocations``
* **invocations_ws**  — ``/invocations_ws`` (duplex WebSocket, streamed deltas)
* **activity**        — ``POST /activity/messages`` and ``POST /api/messages``
* **a2a**             — ``/a2a`` (native A2A JSON-RPC + agent card)

The responses + invocations(+ws) endpoints come from the ``azure-ai-agentserver``
packages composed via cooperative inheritance; the a2a endpoint is the native
Agent Framework / ``a2a`` SDK server; the activity endpoint is a minimal Activity
protocol message exchange. The agent runs the same regardless of protocol, so a
benchmark can compare transports apples-to-apples.

The runner is started lazily on the first request, which is exactly the cold
start the benchmark measures.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.agent_common.a2a_app import build_a2a_app
from src.agent_common.runner import WeatherAgentRunner

logger = logging.getLogger(__name__)


def _extract_text(payload: Any) -> str:
    """Pull user text out of a developer-defined invocation/activity body."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("input", "text", "message", "query", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            content = last.get("content")
            if isinstance(content, str):
                return content
    return ""


def build_host(runner: WeatherAgentRunner, *, public_base_url: str | None = None) -> Any:
    """Compose the multi-protocol AgentServerHost around ``runner``."""
    from azure.ai.agentserver.responses import ResponsesAgentServerHost, TextResponse
    from azure.ai.agentserver.invocations import InvocationAgentServerHost
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route
    from starlette.websockets import WebSocket, WebSocketDisconnect

    public_base_url = public_base_url or os.getenv("PUBLIC_BASE_URL", "http://localhost:8088")

    # ---- Activity protocol (minimal Bot Framework message exchange) ---------
    async def activity_handler(request: Request) -> Response:
        try:
            activity = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if activity.get("type") != "message":
            return Response(status_code=200)
        conversation = (activity.get("conversation") or {}).get("id")
        answer = await runner.run(activity.get("text", "") or "", session_id=conversation)
        reply = {
            "type": "message",
            "text": answer,
            "from": {"id": "weather-agent", "name": "weather-agent"},
            "recipient": activity.get("from"),
            "conversation": activity.get("conversation"),
            "replyToId": activity.get("id"),
        }
        return JSONResponse(reply)

    extra_routes: list[Any] = [
        Route("/activity/messages", activity_handler, methods=["POST"]),
        Route("/api/messages", activity_handler, methods=["POST"]),
    ]

    # ---- A2A (native) -------------------------------------------------------
    a2a_app = build_a2a_app(runner, f"{public_base_url.rstrip('/')}/a2a")
    if a2a_app is not None:
        extra_routes.append(Mount("/a2a", app=a2a_app))

    # ---- Compose responses + invocations(+ws) on one host -------------------
    class WeatherAgentHost(InvocationAgentServerHost, ResponsesAgentServerHost):
        def __init__(self, **kwargs: Any) -> None:
            existing = list(kwargs.pop("routes", None) or [])
            super().__init__(routes=existing + extra_routes, **kwargs)

    app = WeatherAgentHost()

    @app.response_handler
    async def _responses(request: Any, context: Any, cancellation_signal: Any):  # noqa: ANN001
        text = await context.get_input_text()
        session_id = (context.query_parameters or {}).get("agent_session_id")
        return TextResponse(context, request, text=runner.run_stream(text, session_id=session_id))

    @app.invoke_handler
    async def _invoke(request: Request) -> Response:
        data = await request.json()
        session_id = request.query_params.get("agent_session_id")
        if not session_id and isinstance(data, dict):
            session_id = data.get("session_id")
        answer = await runner.run(_extract_text(data), session_id=session_id)
        return JSONResponse({"text": answer})

    @app.ws_handler
    async def _ws(websocket: WebSocket) -> None:
        session_id = websocket.query_params.get("agent_session_id")
        try:
            async for raw in websocket.iter_text():
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "message": "invalid JSON frame"}))
                    continue
                if evt.get("type") != "message":
                    continue
                full = []
                try:
                    async for delta in runner.run_stream(evt.get("text", "") or "", session_id=session_id):
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
        await runner.aclose()

    return app


def run(mode: str | None = None) -> None:
    """Build the runner + multi-protocol host and start serving (blocking)."""
    from src.agent_common.telemetry import configure_telemetry

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    configure_telemetry()
    runner = WeatherAgentRunner(mode=mode)
    app = build_host(runner)
    app.run()
