"""External LangChain weather agent with Responses and A2A endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_core.messages import AIMessageChunk
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

_build_lock = asyncio.Lock()
_server_graph = None
_responses_graph = None

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
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


def _project_endpoint() -> str:
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("Set AZURE_AI_PROJECT_ENDPOINT (or FOUNDRY_PROJECT_ENDPOINT).")
    return endpoint.rstrip("/")


def _endpoint_type() -> str:
    endpoint_type = os.getenv("AZURE_AI_ENDPOINT_TYPE", "foundry").strip().lower()
    if endpoint_type not in {"foundry", "openai"}:
        raise RuntimeError("AZURE_AI_ENDPOINT_TYPE must be 'foundry' or 'openai'.")
    return endpoint_type


def _openai_endpoint() -> str:
    parsed = urlparse(_project_endpoint())
    suffix = ".services.ai.azure.com"
    if not parsed.hostname or not parsed.hostname.endswith(suffix):
        raise RuntimeError("AZURE_AI_PROJECT_ENDPOINT must use a *.services.ai.azure.com host.")
    resource = parsed.hostname[: -len(suffix)]
    return f"{parsed.scheme}://{resource}.openai.azure.com"


def _model_name() -> str:
    return (
        os.getenv("FOUNDRY_MODEL_NAME")
        or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        or "gpt-4.1-mini"
    )


def _build_chat_model(credential: DefaultAzureCredential) -> ChatOpenAI:
    if _endpoint_type() == "openai":
        base_url = f"{_openai_endpoint()}/openai/v1/"
        scope = AZURE_OPENAI_SCOPE
    else:
        project = AIProjectClient(endpoint=_project_endpoint(), credential=credential)
        base_url = str(project.get_openai_client().base_url)
        scope = FOUNDRY_SCOPE
    token_provider = get_bearer_token_provider(credential, scope)
    return ChatOpenAI(
        model=_model_name(),
        base_url=base_url,
        api_key=token_provider,
        use_responses_api=True,
    )


async def build_graph(_config=None):  # noqa: ANN001, ANN201
    """Build the message-state graph exported to LangGraph Agent Server."""
    global _server_graph
    if _server_graph is None:
        async with _build_lock:
            if _server_graph is None:
                _server_graph = await _create_graph()
    return _server_graph


async def build_responses_graph():  # noqa: ANN201
    """Build the checkpointed graph used by the custom Responses route."""
    global _responses_graph
    if _responses_graph is None:
        async with _build_lock:
            if _responses_graph is None:
                _responses_graph = await _create_graph(checkpointer=MemorySaver())
    return _responses_graph


async def _create_graph(checkpointer=None):  # noqa: ANN001, ANN201
    credential = DefaultAzureCredential()
    mcp_client = MultiServerMCPClient(
        {
            "weather": {
                "transport": "streamable_http",
                "url": os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8093/mcp"),
            }
        }
    )
    tools = await mcp_client.get_tools()
    logger.info("Loaded %d tools from direct weather MCP", len(tools))
    graph = create_agent(
        model=_build_chat_model(credential),
        tools=tools,
        system_prompt=INSTRUCTIONS,
        checkpointer=checkpointer,
    )
    return graph


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def build_app() -> Starlette:
    """Build custom routes mounted by LangGraph Agent Server."""
    responses_graph = None

    async def health_handler(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def responses_handler(request: Request):  # noqa: ANN202
        nonlocal responses_graph
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("input"), str):
                raise ValueError("request body must contain a string 'input'")
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        response_id = f"resp_{uuid.uuid4().hex}"
        session_id = (
            request.query_params.get("agent_session_id")
            or body.get("previous_response_id")
            or response_id
        )
        config = {"configurable": {"thread_id": session_id}}
        graph_input = {"messages": [{"role": "user", "content": body["input"]}]}
        if responses_graph is None:
            responses_graph = await build_responses_graph()

        if not body.get("stream", False):
            result = await responses_graph.ainvoke(graph_input, config=config)
            text = _message_text(result["messages"][-1])
            return JSONResponse(
                {
                    "id": response_id,
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "completed",
                    "model": body.get("model", _model_name()),
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                }
            )

        async def events():  # noqa: ANN202
            created = {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "in_progress",
                "model": body.get("model", _model_name()),
                "output": [],
            }
            yield _sse("response.created", {"type": "response.created", "response": created})
            chunks: list[str] = []
            async for message, _metadata in responses_graph.astream(
                graph_input,
                config=config,
                stream_mode="messages",
            ):
                if isinstance(message, AIMessageChunk):
                    delta = _message_text(message)
                    if delta:
                        chunks.append(delta)
                        yield _sse(
                            "response.output_text.delta",
                            {"type": "response.output_text.delta", "delta": delta},
                        )
            completed = {
                **created,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "".join(chunks)}],
                    }
                ],
            }
            yield _sse(
                "response.completed",
                {"type": "response.completed", "response": completed},
            )

        return StreamingResponse(events(), media_type="text/event-stream")

    routes = [
        Route("/health", health_handler, methods=["GET"]),
        Route("/readiness", health_handler, methods=["GET"]),
        Route("/responses", responses_handler, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def main() -> None:
    raise SystemExit("Start this agent with `langgraph dev --config langgraph.json`.")


app = build_app()


if __name__ == "__main__":
    main()
