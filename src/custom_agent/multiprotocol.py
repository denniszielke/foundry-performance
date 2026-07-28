"""Multi-protocol host that exposes the custom weather agent over every protocol.

Self-contained copy (no shared modules). A single
:class:`.runner.WeatherAgentRunner` is served over:

* **responses**       — ``POST /responses`` (OpenAI Responses-shaped, streaming SSE)
* **invocations**     — ``POST /invocations``
* **invocations_ws**  — ``/invocations_ws`` (duplex WebSocket, streamed deltas)
* **activity**        — ``POST /activity/messages`` and ``POST /api/messages``
* **a2a**             — ``/a2a`` (native A2A JSON-RPC + agent card)

Every route here is a plain hand-written Starlette route/handler — this
variation deliberately does NOT use the ``azure-ai-agentserver`` packages (those
are exercised by the hosted-agent variations instead). The responses and
invocations wire formats are just simple enough for the benchmark clients in
``src/clients/protocols.py`` to parse (SSE lines with a top-level ``delta`` key
for responses, ``{"text": ...}`` for invocations); the a2a endpoint is the
native Agent Framework / ``a2a`` SDK server; the activity endpoint is a minimal
Activity protocol message exchange. The agent runs the same regardless of
protocol, so a benchmark can compare transports apples-to-apples.

The runner is started lazily on the first request, which is exactly the cold
start the benchmark measures.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from .a2a_app import build_a2a_routes

if TYPE_CHECKING:
    from .agent import WeatherAgentRunner

logger = logging.getLogger(__name__)


def _extract_text(payload: Any) -> str:
    """Pull user text out of a developer-defined invocation/activity/responses body."""
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
    """Compose a plain Starlette app serving every protocol around ``runner``."""
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect

    public_base_url = public_base_url or os.getenv("PUBLIC_BASE_URL", "http://localhost:8088")

    # ---- responses (OpenAI Responses-shaped, streaming SSE) -----------------
    async def responses_handler(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        text = _extract_text(body)
        session_id = request.query_params.get("agent_session_id")

        async def _sse():
            try:
                async for delta in runner.run_stream(text, session_id=session_id):
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
            except Exception as exc:  # noqa: BLE001 - report per-turn failures
                logger.exception("weather agent responses turn failed")
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    # ---- invocations (plain JSON) --------------------------------------------
    async def invoke_handler(request: Request) -> Response:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        session_id = request.query_params.get("agent_session_id")
        if not session_id and isinstance(data, dict):
            session_id = data.get("session_id")
        answer = await runner.run(_extract_text(data), session_id=session_id)
        return JSONResponse({"text": answer})

    # ---- invocations_ws (duplex WebSocket) -----------------------------------
    async def ws_handler(websocket: WebSocket) -> None:
        await websocket.accept()
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

    async def health_handler(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    routes: list[Any] = [
        Route("/health", health_handler, methods=["GET"]),
        Route("/readiness", health_handler, methods=["GET"]),
        Route("/responses", responses_handler, methods=["POST"]),
        Route("/invocations", invoke_handler, methods=["POST"]),
        WebSocketRoute("/invocations_ws", ws_handler),
        Route("/activity/messages", activity_handler, methods=["POST"]),
        Route("/api/messages", activity_handler, methods=["POST"]),
    ]

    # ---- A2A (native) -------------------------------------------------------
    a2a_routes = build_a2a_routes(runner, f"{public_base_url.rstrip('/')}/a2a")
    if a2a_routes is not None:
        routes.extend(a2a_routes)

    @asynccontextmanager
    async def _lifespan(_: Starlette):
        try:
            yield
        finally:
            await runner.aclose()

    return Starlette(routes=routes, lifespan=_lifespan)


def run() -> None:
    """Build the runner + multi-protocol host and start serving (blocking)."""
    import uvicorn

    from .agent import WeatherAgentRunner
    from .telemetry import configure_telemetry

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting custom weather agent — image_tag=%s", os.getenv("IMAGE_TAG", "unknown"))
    configure_telemetry()
    runner = WeatherAgentRunner()
    app = build_host(runner)
    port = int(os.getenv("PORT", "8088"))
    uvicorn.run(app, host="0.0.0.0", port=port)
