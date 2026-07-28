"""Foundry-hosted weather agent using invocations and Toolbox MCP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a helpful weather assistant. Answer questions about the current "
    "weather and forecast for a city using the available weather tools. Call "
    "list_cities if you are unsure which cities are available. Always call a "
    "tool for real data instead of guessing, and keep answers short."
)
MAX_TOOL_ROUNDS = 10


def _project_endpoint() -> str:
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise EnvironmentError("Set AZURE_AI_PROJECT_ENDPOINT (or FOUNDRY_PROJECT_ENDPOINT).")
    return endpoint.rstrip("/")


def _toolbox_endpoint() -> str:
    endpoint = os.getenv("FOUNDRY_TOOLBOX_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    toolbox_name = os.getenv("WEATHER_TOOLBOX_NAME", "weather-tools")
    return f"{_project_endpoint()}/toolboxes/{toolbox_name}/mcp?api-version=v1"


PROJECT_ENDPOINT = _project_endpoint()
MODEL = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.getenv("FOUNDRY_MODEL") or "gpt-4.1-mini"
TOOLBOX_ENDPOINT = _toolbox_endpoint()

credential = DefaultAzureCredential()
project_client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential)
responses_client = project_client.get_openai_client().responses
token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")


class McpToolboxClient:
    """Small synchronous Toolbox MCP client, modeled on the official sample."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.session_id: str | None = None
        self.request_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token_provider()}",
            "Content-Type": "application/json",
            "Foundry-Features": "Toolboxes=V1Preview",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    def _next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def initialize(self) -> str:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                self.endpoint,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "weather-invocations-agent", "version": "1.0.0"},
                    },
                },
            )
            response.raise_for_status()
            self.session_id = response.headers.get("mcp-session-id")
            payload = response.json()
            initialized = client.post(
                self.endpoint,
                headers=self._headers(),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            initialized.raise_for_status()
        return payload.get("result", {}).get("serverInfo", {}).get("name", "unknown")

    def list_tools(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                self.endpoint,
                headers=self._headers(),
                json={"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}},
            )
            response.raise_for_status()
            return response.json().get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                self.endpoint,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if payload.get("error") or not isinstance(result, dict) or result.get("isError"):
            raise RuntimeError(f"Toolbox returned an error result: {payload}")
        texts = [
            item["text"]
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        return "\n".join(texts) if texts else json.dumps(result)


mcp_client: McpToolboxClient | None = None
tool_definitions: list[dict[str, Any]] = []


def ensure_tools() -> None:
    global mcp_client
    if mcp_client is not None:
        return
    mcp_client = McpToolboxClient(TOOLBOX_ENDPOINT)
    server_name = mcp_client.initialize()
    tools = mcp_client.list_tools()
    if not tools:
        raise RuntimeError("Toolbox returned no tools; cannot produce a grounded answer.")
    tool_definitions.extend(
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for tool in tools
    )
    logger.info("Toolbox '%s' connected: %d tools discovered", server_name, len(tools))


def run_agent_loop(input_items: list[dict[str, Any]]) -> str:
    ensure_tools()
    assert mcp_client is not None
    for round_index in range(MAX_TOOL_ROUNDS):
        response = responses_client.create(
            model=MODEL,
            instructions=INSTRUCTIONS,
            input=input_items,
            tools=tool_definitions,
            tool_choice="required" if round_index == 0 else "auto",
            store=False,
        )
        tool_calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
        if not tool_calls:
            return response.output_text or "(No response)"
        for tool_call in tool_calls:
            try:
                arguments = json.loads(tool_call.arguments) if isinstance(tool_call.arguments, str) else tool_call.arguments
                result = mcp_client.call_tool(tool_call.name, arguments)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Tool '%s' failed", tool_call.name)
                result = f"Error calling tool: {exc}"
            input_items.extend(
                [
                    {
                        "type": "function_call",
                        "id": tool_call.id,
                        "call_id": tool_call.call_id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                    {"type": "function_call_output", "call_id": tool_call.call_id, "output": result},
                ]
            )
    return "(Reached maximum tool call rounds)"


app = InvocationAgentServerHost()
sessions: dict[str, list[dict[str, str]]] = {}


@app.invoke_handler
async def handle_invoke(request: Request) -> StreamingResponse | JSONResponse:
    try:
        payload = await request.json()
        message = payload.get("message") or payload.get("input") or payload.get("query")
        if not isinstance(message, str) or not message.strip():
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "invalid_request", "message": "Provide a non-empty message."})

    session_id = request.state.session_id
    invocation_id = request.state.invocation_id
    history = sessions.setdefault(session_id, [])
    history.append({"role": "user", "content": message})

    async def event_generator():
        try:
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(None, run_agent_loop, list(history))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Invocation %s failed", invocation_id)
            reply = f"Error calling model: {exc}"
        history.append({"role": "assistant", "content": reply})
        yield f"data: {json.dumps({'type': 'token', 'content': reply})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'invocation_id': invocation_id, 'session_id': session_id})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    app.run()