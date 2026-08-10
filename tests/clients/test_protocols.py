import json
import unittest

import httpx

from src.clients.common import Timer
from src.clients.protocols import StoredResponsesClient


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
            await client.call(Timer(), "first", "session-1")
            await client.call(Timer(), "follow-up", "session-1")
        finally:
            await client._http.aclose()

        self.assertEqual(
            requests,
            [
                {"model": "weather", "input": "first", "stream": True, "store": True},
                {
                    "model": "weather",
                    "input": "follow-up",
                    "stream": True,
                    "store": True,
                    "previous_response_id": "resp_1",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()