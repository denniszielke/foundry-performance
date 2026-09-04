import json
import unittest

import httpx

from src.clients.common import Timer
from src.clients.protocols import AgUiInvocationsClient, StoredResponsesClient


class StoredResponsesClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_chains_followup_with_previous_response_id(self) -> None:
        requests: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            response_id = f"resp_{len(requests)}"
            body = (
                'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                f'data: {{"type":"response.completed","response":{{"id":"{response_id}"}}}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        client = StoredResponsesClient("https://example.test", "weather")
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.call(Timer(), "first", "conversation-1", "session-1")
            await client.call(Timer(), "follow-up", "conversation-1", "session-1")
        finally:
            await client._http.aclose()

        self.assertEqual(
            requests,
            [
                {
                    "model": "weather",
                    "input": "first",
                    "stream": True,
                    "agent_session_id": "session-1",
                    "store": True,
                },
                {
                    "model": "weather",
                    "input": "follow-up",
                    "stream": True,
                    "agent_session_id": "session-1",
                    "store": True,
                    "previous_response_id": "resp_1",
                },
            ],
        )

    async def test_invocations_binds_platform_session_in_query_only(self) -> None:
        request_seen: httpx.Request | None = None

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_seen
            request_seen = request
            return httpx.Response(
                200,
                text='data: {"type":"TEXT_MESSAGE_CONTENT","delta":"ok"}\n\n',
                headers={"content-type": "text/event-stream"},
            )

        client = AgUiInvocationsClient("https://example.test")
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.call(Timer(), "hello", "conversation-1", "session-1")
        finally:
            await client._http.aclose()

        assert request_seen is not None
        self.assertEqual(request_seen.url.params.get("agent_session_id"), "session-1")
        self.assertNotIn("agent_session_id", json.loads(request_seen.content))

    async def test_create_session_uses_foundry_session_endpoint_behind_proxy_auth(self) -> None:
        request_seen: httpx.Request | None = None

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_seen
            request_seen = request
            return httpx.Response(201, json={"agent_session_id": "session-1"})

        client = AgUiInvocationsClient(
            "https://example.services.ai.azure.com/api/projects/project/agents/agent/endpoint/protocols"
        )
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            session_id = await client.create_session()
        finally:
            await client._http.aclose()

        assert request_seen is not None
        self.assertEqual(session_id, "session-1")
        self.assertEqual(request_seen.url.path, "/api/projects/project/agents/agent/endpoint/sessions")
        self.assertEqual(request_seen.url.params.get("api-version"), "v1")


if __name__ == "__main__":
    unittest.main()