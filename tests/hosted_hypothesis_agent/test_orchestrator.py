from __future__ import annotations

import os
import unittest

os.environ.setdefault("ALLOW_IN_MEMORY_WORKFLOW_STORE", "true")
os.environ.setdefault("AZURE_AI_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test")

from agent_framework import AgentSession, set_agent_mode  # noqa: E402
from starlette.requests import Request  # noqa: E402

from src.hosted_hypothesis_agent.agent import WorkflowOrchestrator, _caller_id  # noqa: E402
from src.hosted_hypothesis_agent.models import (  # noqa: E402
    ExecutionPlan,
    ExecutionResult,
    Hypothesis,
    PlanStep,
    WorkflowRequest,
    WorkflowStatus,
)
from src.hosted_hypothesis_agent.workflow_store import InMemoryWorkflowStore  # noqa: E402


class FakeRuntime:
    received_call_ids: list[str | None] = []

    def __init__(self, foundry_call_id: str | None = None) -> None:
        self.received_call_ids.append(foundry_call_id)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def create_session(self, session_id: str | None = None) -> AgentSession:
        session = AgentSession(session_id=session_id)
        set_agent_mode(session, "plan", available_modes=("plan", "execute"))
        return session

    async def formulate_hypothesis(self, scenario: str) -> Hypothesis:
        return Hypothesis(statement=f"Test {scenario}", scenario_summary=scenario)

    async def plan(self, scenario, hypothesis, session, revision_comment=None):
        return (
            ExecutionPlan(
                objective="Test the scenario",
                steps=[
                    PlanStep(
                        id=1,
                        title="Research",
                        description="Collect evidence",
                        tool_category="internet_research",
                    )
                ],
            ),
            [],
        )

    async def execute(self, plan, session):
        set_agent_mode(session, "execute", available_modes=("plan", "execute"))
        return ExecutionResult(completed_steps=[1], final_answer="Evidence collected."), []


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def test_caller_id_uses_foundry_protocol_user_header(self) -> None:
        request = Request({"type": "http", "headers": [(b"x-agent-user-id", b"foundry-user-1")]})
        self.assertEqual(_caller_id(request), "foundry-user-1")

    async def test_plan_approve_execute_and_idempotent_retry(self) -> None:
        FakeRuntime.received_call_ids.clear()
        orchestrator = WorkflowOrchestrator(InMemoryWorkflowStore(), runtime_factory=FakeRuntime)
        planned = await orchestrator.dispatch(
            WorkflowRequest(
                action="plan",
                scenario="working scenario",
                client_request_id="plan-1",
            ),
            "caller-1",
            "call-plan-1",
        )
        self.assertEqual(planned.status, WorkflowStatus.AWAITING_APPROVAL)
        self.assertIsNotNone(planned.plan_digest)

        approval = WorkflowRequest(
            action="approve",
            workflow_id=planned.workflow_id,
            client_request_id="approve-1",
            plan_revision=planned.plan_revision,
            plan_digest=planned.plan_digest,
            decision="approved",
        )
        completed = await orchestrator.dispatch(approval, "caller-1", "call-approve-1")
        retried = await orchestrator.dispatch(approval, "caller-1")

        self.assertEqual(completed.status, WorkflowStatus.COMPLETED)
        self.assertEqual(completed.execution.final_answer, "Evidence collected.")
        self.assertEqual(retried.version, completed.version)
        self.assertEqual(FakeRuntime.received_call_ids, ["call-plan-1", "call-approve-1"])

    async def test_different_owner_cannot_read_workflow(self) -> None:
        orchestrator = WorkflowOrchestrator(InMemoryWorkflowStore(), runtime_factory=FakeRuntime)
        planned = await orchestrator.dispatch(
            WorkflowRequest(action="plan", scenario="private", client_request_id="plan-private"),
            "caller-1",
        )
        with self.assertRaises(PermissionError):
            await orchestrator.dispatch(
                WorkflowRequest(action="status", workflow_id=planned.workflow_id, client_request_id="status-1"),
                "caller-2",
            )


if __name__ == "__main__":
    unittest.main()