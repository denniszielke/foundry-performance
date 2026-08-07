"""Validated contracts for the hypothesis workflow."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowStatus(StrEnum):
    HYPOTHESIZING = "hypothesizing"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    statement: str = Field(min_length=1)
    scenario_summary: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    evidence_needed: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    guidance: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    risks_and_constraints: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tool_category: Literal["internet_research", "context_api", "document_search"]
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_evidence: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1)
    steps: list[PlanStep] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_step_ids(self) -> ExecutionPlan:
        expected = list(range(1, len(self.steps) + 1))
        if [step.id for step in self.steps] != expected:
            raise ValueError("plan step IDs must be contiguous and start at 1")
        return self

    def digest(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected", "revise"]
    approver_id: str = Field(min_length=1)
    decided_at: datetime = Field(default_factory=utc_now)
    plan_revision: int = Field(ge=1)
    plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    comment: str | None = Field(default=None, max_length=2000)


class ToolActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal["planning", "execution"]
    tool_category: Literal["internet_research", "context_api", "document_search"]
    tool_name: str
    started_at: datetime
    duration_ms: int = Field(ge=0)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    citations: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_steps: list[int] = Field(default_factory=list)
    final_answer: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkflowRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    owner_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    status: WorkflowStatus = WorkflowStatus.HYPOTHESIZING
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    harness_session: dict[str, Any]
    hypothesis: Hypothesis | None = None
    plan: ExecutionPlan | None = None
    plan_revision: int = 0
    plan_digest: str | None = None
    approval: ApprovalRecord | None = None
    tool_activity: list[ToolActivity] = Field(default_factory=list)
    execution: ExecutionResult | None = None
    error: str | None = None
    processed_request_ids: dict[str, dict[str, Any]] = Field(default_factory=dict)
    version: int = 0

    def assert_owner(self, owner_id: str) -> None:
        if owner_id != self.owner_id:
            raise PermissionError("workflow belongs to a different caller")

    def set_plan(self, hypothesis: Hypothesis, plan: ExecutionPlan) -> None:
        if self.status not in {
            WorkflowStatus.HYPOTHESIZING,
            WorkflowStatus.PLANNING,
            WorkflowStatus.AWAITING_APPROVAL,
        }:
            raise ValueError(f"cannot plan workflow in status {self.status}")
        self.hypothesis = hypothesis
        self.plan = plan
        self.plan_revision += 1
        self.plan_digest = plan.digest()
        self.approval = None
        self.status = WorkflowStatus.AWAITING_APPROVAL
        self.updated_at = utc_now()

    def decide(self, approval: ApprovalRecord) -> None:
        if self.status != WorkflowStatus.AWAITING_APPROVAL:
            raise ValueError(f"workflow is not awaiting approval: {self.status}")
        if approval.plan_revision != self.plan_revision or approval.plan_digest != self.plan_digest:
            raise ValueError("approval does not match the current plan revision and digest")
        self.approval = approval
        self.status = {
            "approved": WorkflowStatus.APPROVED,
            "rejected": WorkflowStatus.REJECTED,
            "revise": WorkflowStatus.PLANNING,
        }[approval.decision]
        self.updated_at = utc_now()

    def begin_execution(self) -> None:
        if self.status != WorkflowStatus.APPROVED or not self.approval:
            raise ValueError("workflow does not have a valid approval")
        self.status = WorkflowStatus.EXECUTING
        self.updated_at = utc_now()


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["plan", "approve", "status"]
    workflow_id: str | None = None
    scenario: str | None = Field(default=None, max_length=50_000)
    client_request_id: str = Field(min_length=1, max_length=200)
    plan_revision: int | None = Field(default=None, ge=1)
    plan_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["approved", "rejected", "revise"] | None = None
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_action_fields(self) -> WorkflowRequest:
        if self.action == "plan" and not self.scenario and not self.workflow_id:
            raise ValueError("a new plan requires scenario")
        if self.action in {"approve", "status"} and not self.workflow_id:
            raise ValueError(f"{self.action} requires workflow_id")
        if self.action == "approve" and (
            self.plan_revision is None or self.plan_digest is None or self.decision is None
        ):
            raise ValueError("approve requires plan_revision, plan_digest, and decision")
        return self