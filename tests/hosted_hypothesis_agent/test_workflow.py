from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agent_framework import AgentSession

from src.hosted_hypothesis_agent.models import (
    ApprovalRecord,
    ExecutionPlan,
    Hypothesis,
    PlanStep,
    WorkflowRecord,
    WorkflowStatus,
    utc_now,
)
from src.hosted_hypothesis_agent.workflow_store import (
    BlobWorkflowStore,
    FoundryCallIdPolicy,
    InMemoryWorkflowStore,
    WorkflowConflictError,
    foundry_request_context,
)


def _hypothesis() -> Hypothesis:
    return Hypothesis(statement="Evidence supports the scenario.", scenario_summary="A scenario")


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        objective="Test the hypothesis",
        steps=[
            PlanStep(
                id=1,
                title="Research",
                description="Find current public evidence",
                tool_category="internet_research",
            )
        ],
    )


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def test_foundry_call_id_policy_uses_request_context(self) -> None:
        request = SimpleNamespace(http_request=SimpleNamespace(headers={}))
        policy = FoundryCallIdPolicy()
        policy.on_request(request)
        self.assertNotIn("x-agent-foundry-call-id", request.http_request.headers)

        with foundry_request_context("call-1"):
            policy.on_request(request)
        self.assertEqual(request.http_request.headers["x-agent-foundry-call-id"], "call-1")

    async def test_blob_store_uses_json_content_settings_and_etag(self) -> None:
        service = MagicMock()
        container = MagicMock()
        blob = MagicMock()
        service.get_container_client.return_value = container
        container.get_blob_client.return_value = blob
        container.create_container = AsyncMock()
        blob.upload_blob = AsyncMock()

        record = WorkflowRecord(
            owner_id="caller-1",
            scenario="A scenario",
            expires_at=utc_now() + timedelta(hours=1),
            harness_session=AgentSession().to_dict(),
        )
        downloader = MagicMock()
        downloader.readall = AsyncMock(return_value=record.model_dump_json().encode())
        downloader.properties = SimpleNamespace(etag='"etag-1"')
        blob.download_blob = AsyncMock(return_value=downloader)

        with patch("azure.storage.blob.aio.BlobServiceClient", return_value=service):
            store = BlobWorkflowStore("https://storage.example", credential=object())
        await store.create(record)
        created_kwargs = blob.upload_blob.await_args.kwargs
        self.assertEqual(created_kwargs["content_settings"].content_type, "application/json")

        updated = await store.mutate(record.workflow_id, 0, lambda item: setattr(item, "error", "updated"))
        mutation_kwargs = blob.upload_blob.await_args.kwargs
        self.assertEqual(updated.version, 1)
        self.assertEqual(mutation_kwargs["etag"], '"etag-1"')
        self.assertEqual(mutation_kwargs["content_settings"].content_type, "application/json")

    async def test_approval_is_bound_to_plan_and_execution_is_single_transition(self) -> None:
        session = AgentSession(session_id="session-1")
        session.state["agent_mode"] = {"current_mode": "plan"}
        store = InMemoryWorkflowStore()
        record = WorkflowRecord(
            owner_id="caller-1",
            scenario="A scenario",
            expires_at=utc_now() + timedelta(hours=1),
            harness_session=session.to_dict(),
        )
        record.set_plan(_hypothesis(), _plan())
        created = await store.create(record)

        approval = ApprovalRecord(
            decision="approved",
            approver_id="caller-1",
            plan_revision=created.plan_revision,
            plan_digest=created.plan_digest or "",
        )
        approved = await store.mutate(created.workflow_id, created.version, lambda item: item.decide(approval))
        executing = await store.mutate(approved.workflow_id, approved.version, lambda item: item.begin_execution())

        self.assertEqual(executing.status, WorkflowStatus.EXECUTING)
        self.assertEqual(AgentSession.from_dict(executing.harness_session).session_id, "session-1")
        with self.assertRaises(WorkflowConflictError):
            await store.mutate(approved.workflow_id, approved.version, lambda item: item.begin_execution())

    def test_plan_digest_is_stable_and_revision_sensitive(self) -> None:
        first = _plan()
        second = _plan().model_copy(deep=True)
        self.assertEqual(first.digest(), second.digest())
        second.steps[0].description = "Changed scope"
        self.assertNotEqual(first.digest(), second.digest())

    def test_plan_rejects_more_than_five_steps(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 5 items"):
            ExecutionPlan(
                objective="Oversized plan",
                steps=[
                    PlanStep(
                        id=index,
                        title=f"Step {index}",
                        description="Collect evidence",
                        tool_category="internet_research",
                    )
                    for index in range(1, 7)
                ],
            )

    def test_stale_approval_is_rejected(self) -> None:
        record = WorkflowRecord(
            owner_id="caller-1",
            scenario="A scenario",
            expires_at=utc_now() + timedelta(hours=1),
            harness_session=AgentSession().to_dict(),
        )
        record.set_plan(_hypothesis(), _plan())
        with self.assertRaisesRegex(ValueError, "current plan revision and digest"):
            record.decide(
                ApprovalRecord(
                    decision="approved",
                    approver_id="caller-1",
                    plan_revision=record.plan_revision,
                    plan_digest="sha256:" + "0" * 64,
                )
            )


if __name__ == "__main__":
    unittest.main()