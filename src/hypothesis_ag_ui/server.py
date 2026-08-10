"""Local AG-UI adapter and static host for the hypothesis workflow frontend."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "frontend" / "dist"
SCOPE = "https://ai.azure.com/.default"
logger = logging.getLogger(__name__)

load_dotenv(ROOT.parent.parent / ".env", override=False)


class BearerAuth(httpx.Auth):
    def __init__(self) -> None:
        credential = DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(credential, SCOPE)

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._token_provider()}"
        yield request


def upstream_url() -> str:
    if explicit := os.getenv("HYPOTHESIS_AGENT_INVOCATIONS_URL", "").strip():
        return explicit.rstrip("/")
    project = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "").strip().rstrip("/")
    if not project:
        raise RuntimeError(
            "Set HYPOTHESIS_AGENT_INVOCATIONS_URL or AZURE_AI_PROJECT_ENDPOINT."
        )
    agent = os.getenv(
        "HYPOTHESIS_HOSTED_AGENT_NAME", "scenario-hosted-hypothesis-agent"
    )
    return f"{project}/agents/{agent}/endpoint/protocols/invocations"


@asynccontextmanager
async def lifespan(app: FastAPI):
    existing = getattr(app.state, "http", None)
    if existing is not None:
        yield
        return
    try:
        app.state.http = httpx.AsyncClient(auth=BearerAuth(), timeout=300.0)
    except ImportError as exc:
        if "socksio" not in str(exc):
            raise
        logger.warning("SOCKS proxy support is unavailable; using direct HTTP")
        app.state.http = httpx.AsyncClient(
            auth=BearerAuth(), timeout=300.0, trust_env=False
        )
    try:
        yield
    finally:
        await app.state.http.aclose()
        del app.state.http


app = FastAPI(title="Hypothesis AG-UI", lifespan=lifespan)


def event_stream(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def workflow_control(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("workflow"), dict):
        raise HTTPException(422, "RunAgentInput.state.workflow must be an object")
    return state["workflow"]


async def invoke_workflow(request: Request, control: dict[str, Any]) -> dict[str, Any]:
    response = await request.app.state.http.post(
        upstream_url(), params={"api-version": "v1"}, json=control
    )
    if response.is_error:
        try:
            detail = response.json().get("error", response.text)
        except (ValueError, AttributeError):
            detail = response.text
        raise HTTPException(response.status_code, str(detail))
    result = response.json()
    if not isinstance(result, dict):
        raise HTTPException(502, "Hypothesis agent returned a non-object response")
    return result


@app.post("/api/agent")
async def run_agent(request: Request) -> StreamingResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(422, "AG-UI request must be an object")
    thread_id = str(payload.get("threadId") or "hypothesis-workflow")
    run_id = str(payload.get("runId") or "workflow-run")
    control = workflow_control(payload)
    result = await invoke_workflow(request, control)

    async def events() -> AsyncIterator[str]:
        yield event_stream(
            {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}
        )
        yield event_stream({"type": "STATE_SNAPSHOT", "snapshot": {"workflow": result}})
        yield event_stream(
            {
                "type": "RUN_FINISHED",
                "threadId": thread_id,
                "runId": run_id,
                "result": result,
            }
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        candidate = (DIST / path).resolve()
        if path and candidate.is_relative_to(DIST) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "5178")))