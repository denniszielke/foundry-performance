"""Foundry-hosted hypothesis workflow over the invocations protocol."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from agent_framework import AgentSession
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.identity import DefaultAzureCredential
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .models import (
    ApprovalRecord,
    WorkflowRecord,
    WorkflowRequest,
    WorkflowStatus,
    utc_now,
)
from .runtime import AgentRuntime
from .workflow_store import (
    BlobWorkflowStore,
    InMemoryWorkflowStore,
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowStore,
    foundry_request_context,
)

logger = logging.getLogger(__name__)


def _build_store() -> WorkflowStore:
    account_url = os.getenv("WORKFLOW_STORAGE_ACCOUNT_URL", "").strip()
    if account_url:
        return BlobWorkflowStore(
            account_url=account_url,
            credential=DefaultAzureCredential(),
            container_name=os.getenv("WORKFLOW_STORAGE_CONTAINER", "agent-workflows"),
        )
    if os.getenv("ALLOW_IN_MEMORY_WORKFLOW_STORE", "").lower() in {"1", "true", "yes"}:
        logger.warning("Using non-durable in-memory workflow store")
        return InMemoryWorkflowStore()
    raise RuntimeError("Set WORKFLOW_STORAGE_ACCOUNT_URL for durable workflow state.")


def _caller_id(request: Request) -> str:
    if value := request.headers.get("x-agent-user-id"):
        return value
    if os.getenv("ALLOW_INSECURE_LOCAL_CALLER", "").lower() in {"1", "true", "yes"}:
        return request.headers.get("x-caller-id", "local-development")
    raise PermissionError("authenticated caller identity is unavailable")


async def _workflow_request(request: Request) -> WorkflowRequest:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    state = payload.get("state")
    if isinstance(state, dict):
        control = state.get("workflow") if isinstance(state.get("workflow"), dict) else state
    else:
        control = payload
    return WorkflowRequest.model_validate(control)


def _envelope(record: WorkflowRecord) -> dict[str, Any]:
    return {
        "workflow_id": record.workflow_id,
        "status": record.status,
        "hypothesis": record.hypothesis.model_dump(mode="json") if record.hypothesis else None,
        "plan": record.plan.model_dump(mode="json") if record.plan else None,
        "plan_revision": record.plan_revision,
        "plan_digest": record.plan_digest,
        "approval": record.approval.model_dump(mode="json") if record.approval else None,
        "tool_activity": [item.model_dump(mode="json") for item in record.tool_activity],
        "execution": record.execution.model_dump(mode="json") if record.execution else None,
        "error": record.error,
    }


class WorkflowOrchestrator:
    def __init__(self, store: WorkflowStore, runtime_factory: Callable[..., AgentRuntime] = AgentRuntime) -> None:
        self.store = store
        self.runtime_factory = runtime_factory

    def _runtime(self, foundry_call_id: str | None) -> AgentRuntime:
        if foundry_call_id:
            return self.runtime_factory(foundry_call_id=foundry_call_id)
        return self.runtime_factory()

    async def dispatch(
        self,
        control: WorkflowRequest,
        owner_id: str,
        foundry_call_id: str | None = None,
    ) -> WorkflowRecord:
        if control.action == "status":
            record = await self.store.get(control.workflow_id or "")
            record.assert_owner(owner_id)
            return record
        if control.action == "plan":
            return await self._plan(control, owner_id, foundry_call_id)
        return await self._decide(control, owner_id, foundry_call_id)

    async def _plan(
        self,
        control: WorkflowRequest,
        owner_id: str,
        foundry_call_id: str | None = None,
    ) -> WorkflowRecord:
        if control.workflow_id:
            record = await self.store.get(control.workflow_id)
            record.assert_owner(owner_id)
            revision_comment = control.comment
        else:
            deterministic_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{owner_id}:{control.client_request_id}").hex
            async with self._runtime(foundry_call_id) as runtime:
                session = runtime.create_session(session_id=deterministic_id)
                record = WorkflowRecord(
                    workflow_id=deterministic_id,
                    owner_id=owner_id,
                    scenario=control.scenario or "",
                    expires_at=utc_now() + timedelta(hours=int(os.getenv("WORKFLOW_TTL_HOURS", "24"))),
                    harness_session=session.to_dict(),
                )
                try:
                    record = await self.store.create(record)
                except WorkflowConflictError:
                    existing = await self.store.get(deterministic_id)
                    existing.assert_owner(owner_id)
                    return existing
                hypothesis = await runtime.formulate_hypothesis(record.scenario)
                plan, activities = await runtime.plan(record.scenario, hypothesis, session)
                return await self.store.mutate(
                    record.workflow_id,
                    record.version,
                    lambda item: self._store_plan(item, hypothesis, plan, activities, session),
                )

        if record.status not in {WorkflowStatus.PLANNING, WorkflowStatus.AWAITING_APPROVAL}:
            raise ValueError(f"workflow cannot be revised in status {record.status}")
        session = AgentSession.from_dict(record.harness_session)
        async with self._runtime(foundry_call_id) as runtime:
            hypothesis = await runtime.formulate_hypothesis(record.scenario)
            plan, activities = await runtime.plan(record.scenario, hypothesis, session, revision_comment)
        return await self.store.mutate(
            record.workflow_id,
            record.version,
            lambda item: self._store_plan(item, hypothesis, plan, activities, session),
        )

    @staticmethod
    def _store_plan(record: WorkflowRecord, hypothesis: Any, plan: Any, activities: list[Any], session: AgentSession) -> None:
        record.set_plan(hypothesis, plan)
        record.harness_session = session.to_dict()
        record.tool_activity.extend(activities)

    async def _decide(
        self,
        control: WorkflowRequest,
        owner_id: str,
        foundry_call_id: str | None = None,
    ) -> WorkflowRecord:
        record = await self.store.get(control.workflow_id or "")
        record.assert_owner(owner_id)
        if control.client_request_id in record.processed_request_ids:
            return record
        if record.expires_at <= utc_now():
            raise ValueError("workflow has expired")
        if record.status in {WorkflowStatus.COMPLETED, WorkflowStatus.REJECTED}:
            return record
        approval = ApprovalRecord(
            decision=control.decision or "rejected",
            approver_id=owner_id,
            plan_revision=control.plan_revision or 0,
            plan_digest=control.plan_digest or "",
            comment=control.comment,
        )
        decided = await self.store.mutate(
            record.workflow_id,
            record.version,
            lambda item: self._record_decision(item, approval, control.client_request_id),
        )
        if approval.decision == "revise":
            revision_request = WorkflowRequest(
                action="plan",
                workflow_id=record.workflow_id,
                client_request_id=control.client_request_id,
                comment=control.comment,
            )
            return await self._plan(revision_request, owner_id, foundry_call_id)
        if approval.decision == "rejected":
            return decided

        executing = await self.store.mutate(
            decided.workflow_id,
            decided.version,
            lambda item: item.begin_execution(),
        )
        session = AgentSession.from_dict(executing.harness_session)
        if executing.plan is None:
            raise ValueError("approved workflow has no plan")
        try:
            async with self._runtime(foundry_call_id) as runtime:
                result, activities = await runtime.execute(executing.plan, session)
            return await self.store.mutate(
                executing.workflow_id,
                executing.version,
                lambda item: self._complete(item, result, activities, session),
            )
        except Exception as exc:
            logger.exception("Workflow execution failed: %s", executing.workflow_id)
            await self.store.mutate(
                executing.workflow_id,
                executing.version,
                lambda item: self._fail(item, exc, session),
            )
            raise

    @staticmethod
    def _record_decision(record: WorkflowRecord, approval: ApprovalRecord, request_id: str) -> None:
        record.decide(approval)
        record.processed_request_ids[request_id] = {
            "action": "approve",
            "decision": approval.decision,
            "plan_revision": approval.plan_revision,
            "plan_digest": approval.plan_digest,
        }

    @staticmethod
    def _complete(record: WorkflowRecord, result: Any, activities: list[Any], session: AgentSession) -> None:
        if record.status != WorkflowStatus.EXECUTING:
            raise ValueError(f"workflow is not executing: {record.status}")
        record.execution = result
        record.tool_activity.extend(activities)
        record.harness_session = session.to_dict()
        record.status = WorkflowStatus.COMPLETED

    @staticmethod
    def _fail(record: WorkflowRecord, error: Exception, session: AgentSession) -> None:
        record.error = f"{type(error).__name__}: {error}"[:4000]
        record.harness_session = session.to_dict()
        record.status = WorkflowStatus.FAILED


store = _build_store()
orchestrator = WorkflowOrchestrator(store)
app = InvocationAgentServerHost()


@app.invoke_handler
async def handle_invoke(request: Request) -> Response:
    try:
        control = await _workflow_request(request)
        foundry_call_id = request.headers.get("x-agent-foundry-call-id")
        with foundry_request_context(foundry_call_id):
            record = await orchestrator.dispatch(
                control,
                _caller_id(request),
                foundry_call_id,
            )
        return JSONResponse(_envelope(record))
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except WorkflowNotFoundError as exc:
        return JSONResponse({"error": f"workflow not found: {exc.args[0]}"}, status_code=404)
    except WorkflowConflictError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (ValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled workflow invocation failure")
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    app.run()