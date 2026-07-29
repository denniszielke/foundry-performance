"""Native multi-protocol host for the custom MAF weather agent.

One Microsoft Agent Framework agent is exposed through the framework-neutral
Responses and A2A hosting packages. This external container owns the Starlette
server plus its Invocations and WebSocket routes.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from .a2a_app import build_a2a_routes

logger = logging.getLogger(__name__)


def build_host(agent: Any, *, public_base_url: str | None = None) -> Any:
    """Compose all protocol routes around one native Agent Framework agent."""
    from agent_framework_hosting_responses import (
        create_response_id,
        responses_from_run,
        responses_from_streaming_run,
        responses_session_id,
        responses_to_run,
    )
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route
    from starlette.websockets import WebSocket, WebSocketDisconnect

    public_base_url = public_base_url or os.getenv("PUBLIC_BASE_URL", "http://localhost:8088")
    sessions: dict[str, Any] = {}

    def session_for(session_id: str | None = None) -> Any:
        if not session_id:
            return agent.create_session()
        session = sessions.get(session_id)
        if session is None:
            session = agent.create_session(session_id=session_id)
            sessions[session_id] = session
        return session

    async def responses_handler(request: Request) -> Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            run_args = responses_to_run(body)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        response_id = create_response_id()
        request_session_id, is_conversation = responses_session_id(body)
        session_key = request.query_params.get("agent_session_id") or request_session_id or response_id
        session = session_for(session_key)
        sessions[response_id] = session
        conversation_id = request_session_id if is_conversation else None

        if run_args["stream"]:
            stream = agent.run(
                run_args["messages"],
                stream=True,
                session=session,
                options=run_args["options"],
            )
            return StreamingResponse(
                responses_from_streaming_run(
                    stream,
                    response_id=response_id,
                    conversation_id=conversation_id,
                ),
                media_type="text/event-stream",
            )

        result = await agent.run(
            run_args["messages"],
            session=session,
            options=run_args["options"],
        )
        return JSONResponse(
            responses_from_run(
                result,
                response_id=response_id,
                conversation_id=conversation_id,
            )
        )

    async def invocations_handler(request: Request) -> Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            message = body.get("message") or body.get("input")
            if not isinstance(message, str) or not message:
                raise ValueError("request body must contain a non-empty 'message' or 'input'")
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        session_id = request.query_params.get("agent_session_id") or body.get("session_id")
        result = await agent.run(message, session=session_for(session_id))
        return Response(result.text or "", media_type="text/plain")

    async def health_handler(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    routes: list[Any] = [
        Route("/health", health_handler, methods=["GET"]),
        Route("/readiness", health_handler, methods=["GET"]),
        Route("/responses", responses_handler, methods=["POST"]),
        Route("/invocations", invocations_handler, methods=["POST"]),
        *build_a2a_routes(agent, f"{public_base_url.rstrip('/')}/a2a"),
    ]

    async def ws_handler(websocket: WebSocket) -> None:
        await websocket.accept()
        session = session_for(websocket.query_params.get("agent_session_id"))
        try:
            async for raw in websocket.iter_text():
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "invalid JSON frame"})
                    continue
                if event.get("type") != "message":
                    continue

                chunks: list[str] = []
                try:
                    async for update in agent.run(event.get("text", "") or "", stream=True, session=session):
                        if update.text:
                            chunks.append(update.text)
                            await websocket.send_json({"type": "delta", "text": update.text})
                    await websocket.send_json({"type": "done", "text": "".join(chunks)})
                except Exception as exc:  # noqa: BLE001 - report turn failures
                    logger.exception("weather MAF agent WebSocket turn failed")
                    await websocket.send_json({"type": "error", "message": str(exc)})
        except WebSocketDisconnect:
            pass

    from starlette.routing import WebSocketRoute

    routes.append(WebSocketRoute("/invocations_ws", ws_handler))

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with agent:
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def run() -> None:
    """Build the native agent and start the multi-protocol server."""
    import uvicorn

    from .agent import build_agent

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting custom MAF weather agent - image_tag=%s", os.getenv("IMAGE_TAG", "unknown"))
    uvicorn.run(build_host(build_agent()), host="0.0.0.0", port=int(os.getenv("PORT", "8088")))
