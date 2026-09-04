import unittest

from src.clients.protocols import ResponsesClient
from src.clients.run_benchmark import _bench_protocol


class FakeHostedClient(ResponsesClient):
    instances: list["FakeHostedClient"] = []

    def __init__(self, base_url: str, model: str, auth=None) -> None:
        super().__init__(base_url, model, auth)
        self.created_sessions: list[str] = []
        self.calls: list[tuple[str | None, str | None]] = []
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def create_session(self) -> str:
        session_id = f"session-{len(self.created_sessions) + 1}"
        self.created_sessions.append(session_id)
        return session_id

    async def call(
        self,
        timer,
        query: str,
        conversation_id: str | None = None,
        agent_session_id: str | None = None,
    ) -> str:
        self.calls.append((conversation_id, agent_session_id))
        return "ok"


class SessionModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeHostedClient.instances.clear()

    async def test_dedicated_creates_one_platform_session_per_workload(self) -> None:
        await _bench_protocol(
            "responses",
            "https://example.test/protocols/openai",
            "weather",
            2,
            "weather?",
            None,
            "dedicated",
            FakeHostedClient,
        )

        client = FakeHostedClient.instances[-1]
        self.assertEqual(client.created_sessions, [f"session-{index}" for index in range(1, 6)])
        self.assertEqual([session for _, session in client.calls], [
            "session-1", "session-2", "session-3", "session-4", "session-4", "session-5", "session-5",
        ])

    async def test_shared_reuses_one_platform_session_without_sharing_conversations(self) -> None:
        await _bench_protocol(
            "responses",
            "https://example.test/protocols/openai",
            "weather",
            2,
            "weather?",
            None,
            "shared",
            FakeHostedClient,
        )

        client = FakeHostedClient.instances[-1]
        self.assertEqual(client.created_sessions, ["session-1"])
        self.assertEqual({session for _, session in client.calls}, {"session-1"})
        conversations = [conversation for conversation, _ in client.calls]
        self.assertEqual(conversations[3], conversations[4])
        self.assertEqual(conversations[5], conversations[6])
        self.assertEqual(len(set(conversations[:4])), 4)


if __name__ == "__main__":
    unittest.main()