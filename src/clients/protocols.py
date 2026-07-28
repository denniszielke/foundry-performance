"""Protocol clients for the weather-agent benchmark.

One client per transport the agents expose. They all implement the same tiny
async interface so :mod:`run_benchmark` can drive them uniformly::

    async with ResponsesClient(base_url, model) as c:
        text = await c.call(timer, "weather in Berlin?", session_id)

``call`` runs one turn, marks ``timer.first_byte()`` when the first streamed
token arrives (for protocols that stream), and returns the final answer text.
``session_id`` reuses conversation state so the harness can compare a first
request against a follow-up.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from src.clients.common import Timer

DEFAULT_TIMEOUT = httpx.Timeout(120.0)


def _find_text(obj: Any) -> str:
    """Best-effort recursive extraction of assistant text from a JSON blob."""
    if isinstance(obj, str):
        return obj
    parts: list[str] = []
    if isinstance(obj, dict):
        for key in ("text", "content"):
            value = obj.get(key)
            if isinstance(value, str):
                parts.append(value)
        for key, value in obj.items():
            if key in ("text", "content"):
                continue
            if isinstance(value, (dict, list)):
                found = _find_text(value)
                if found:
                    parts.append(found)
    elif isinstance(obj, list):
        for item in obj:
            found = _find_text(item)
            if found:
                parts.append(found)
    return " ".join(p for p in parts if p).strip()


class _HttpClient:
    """Base class managing a shared ``httpx.AsyncClient``."""

    name = "base"
    supports_ttfb = False

    def __init__(self, base_url: str, auth: httpx.Auth | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = auth
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, auth=self._auth)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        assert self._http is not None, "client not entered"
        return self._http

    def _foundry_params(self) -> dict[str, str]:
        """Extra query params Foundry's agent endpoint requires (not needed by the
        anonymous custom-agent Container App)."""
        return {"api-version": "v1"} if self._auth is not None else {}

    async def call(self, timer: Timer, query: str, session_id: str | None = None) -> str:
        raise NotImplementedError


class ResponsesClient(_HttpClient):
    """OpenAI Responses protocol (``POST /responses``, streaming SSE)."""

    name = "responses"
    supports_ttfb = True

    def __init__(self, base_url: str, model: str = "weather-agent", auth: httpx.Auth | None = None) -> None:
        super().__init__(base_url, auth=auth)
        self.model = model

    async def call(self, timer: Timer, query: str, session_id: str | None = None) -> str:
        params = self._foundry_params()
        if session_id:
            params["agent_session_id"] = session_id
        body = {"model": self.model, "input": query, "stream": True}
        chunks: list[str] = []
        async with self.http.stream("POST", f"{self.base_url}/responses", json=body, params=params or None) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = obj.get("delta") if isinstance(obj, dict) else None
                if isinstance(delta, str) and delta:
                    timer.first_byte()
                    chunks.append(delta)
        return "".join(chunks)


class InvocationsClient(_HttpClient):
    """Native invocations protocol (``POST /invocations``, JSON)."""

    name = "invocations"

    async def call(self, timer: Timer, query: str, session_id: str | None = None) -> str:
        params = self._foundry_params()
        if session_id:
            params["agent_session_id"] = session_id
        response = await self.http.post(
            f"{self.base_url}/invocations", json={"input": query}, params=params or None
        )
        response.raise_for_status()
        return _find_text(response.json())


class AgUiInvocationsClient(_HttpClient):
    """AG-UI over the invocations protocol (``POST /invocations``, SSE)."""

    def __init__(self, base_url: str, auth: httpx.Auth | None = None) -> None:
        super().__init__(base_url, auth=auth)
        self._histories: dict[str, list[dict[str, str]]] = {}

    async def call(self, timer: Timer, query: str, session_id: str | None = None) -> str:
        params = self._foundry_params()
        thread_id = session_id or uuid.uuid4().hex
        history = self._histories.setdefault(thread_id, [])
        history.append({"id": uuid.uuid4().hex, "role": "user", "content": query})
        body = {
            "threadId": thread_id,
            "runId": uuid.uuid4().hex,
            "messages": history,
            "tools": [],
            "context": [],
            "state": {},
        }
        chunks: list[str] = []
        async with self.http.stream(
            "POST",
            f"{self.base_url}/invocations",
            json=body,
            params=params or None,
            headers={"Accept": "text/event-stream"},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "TEXT_MESSAGE_CONTENT" and isinstance(event.get("delta"), str):
                    timer.first_byte()
                    chunks.append(event["delta"])
        reply = "".join(chunks)
        if reply:
            history.append({"id": uuid.uuid4().hex, "role": "assistant", "content": reply})
        return reply


class InvocationsWsClient(_HttpClient):
    """Custom invocations protocol over WebSocket (``/invocations_ws``, streamed)."""

    name = "invocations_ws"
    supports_ttfb = True

    def _ws_url(self, session_id: str | None) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[-1]
        params = self._foundry_params()
        if session_id:
            params["agent_session_id"] = session_id
        query = f"?{urlencode(params)}" if params else ""
        return f"{scheme}://{host}/invocations_ws{query}"

    async def call(self, timer: Timer, query: str, session_id: str | None = None) -> str:
        import websockets

        from src.clients.auth import EntraTokenAuth

        headers = self._auth.header() if isinstance(self._auth, EntraTokenAuth) else None
        chunks: list[str] = []
        async with websockets.connect(self._ws_url(session_id), additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "message", "text": query}))
            async for raw in ws:
                evt = json.loads(raw)
                kind = evt.get("type")
                if kind == "delta":
                    timer.first_byte()
                    chunks.append(evt.get("text", ""))
                elif kind == "done":
                    return evt.get("text") or "".join(chunks)
                elif kind == "error":
                    raise RuntimeError(evt.get("message", "ws error"))
        return "".join(chunks)


class A2aClient(_HttpClient):
    """Native A2A JSON-RPC (``POST /a2a``, ``message/send``)."""

    name = "a2a"

    async def call(self, timer: Timer, query: str, session_id: str | None = None) -> str:
        message: dict[str, Any] = {
            "role": "user",
            "parts": [{"kind": "text", "type": "text", "text": query}],
            "messageId": uuid.uuid4().hex,
            "kind": "message",
        }
        if session_id:
            message["contextId"] = session_id
        rpc = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "message/send",
            "params": {"message": message},
        }
        resp = await self.http.post(f"{self.base_url}/a2a", json=rpc, params=self._foundry_params() or None)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data["error"]))
        return _find_text(data.get("result") if isinstance(data, dict) else data)


class ActivityClient(_HttpClient):
    """Bot Framework Activity protocol (``POST /activity/messages``)."""

    name = "activity"

    async def call(self, timer: Timer, query: str, session_id: str | None = None) -> str:
        activity = {
            "type": "message",
            "text": query,
            "id": uuid.uuid4().hex,
            "from": {"id": "benchmark-user"},
            "conversation": {"id": session_id or uuid.uuid4().hex},
        }
        resp = await self.http.post(f"{self.base_url}/activity/messages", json=activity)
        resp.raise_for_status()
        return _find_text(resp.json())


CLIENTS: dict[str, type[_HttpClient]] = {
    ResponsesClient.name: ResponsesClient,
    InvocationsClient.name: InvocationsClient,
    InvocationsWsClient.name: InvocationsWsClient,
    A2aClient.name: A2aClient,
    ActivityClient.name: ActivityClient,
}
