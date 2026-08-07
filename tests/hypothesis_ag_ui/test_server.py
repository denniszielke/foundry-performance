"""Focused contract tests for the AG-UI invocations adapter."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.hypothesis_ag_ui.server import app


class AdapterTests(unittest.TestCase):
    def test_streams_workflow_as_ag_ui_state(self) -> None:
        workflow = {
            "workflow_id": "workflow-1",
            "status": "awaiting_approval",
            "plan_revision": 1,
            "plan_digest": "sha256:" + "a" * 64,
        }
        response = SimpleNamespace(
            is_error=False,
            json=lambda: workflow,
        )
        app.state.http = SimpleNamespace(post=AsyncMock(return_value=response))
        with TestClient(app) as client:
            result = client.post(
                "/api/agent",
                json={
                    "threadId": "thread-1",
                    "runId": "run-1",
                    "messages": [],
                    "tools": [],
                    "context": [],
                    "state": {
                        "workflow": {
                            "action": "plan",
                            "scenario": "Test a claim",
                            "client_request_id": "request-1",
                        }
                    },
                },
            )

        self.assertEqual(result.status_code, 200)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in result.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [event["type"] for event in events],
            ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"],
        )
        self.assertEqual(events[1]["snapshot"]["workflow"], workflow)
        request_body = app.state.http.post.await_args.kwargs["json"]
        self.assertEqual(request_body["scenario"], "Test a claim")
        del app.state.http

    def test_rejects_missing_workflow_state(self) -> None:
        app.state.http = SimpleNamespace(post=AsyncMock())
        with TestClient(app) as client:
            result = client.post(
                "/api/agent",
                json={"threadId": "thread-1", "runId": "run-1", "state": {}},
            )
        self.assertEqual(result.status_code, 422)
        del app.state.http


if __name__ == "__main__":
    unittest.main()