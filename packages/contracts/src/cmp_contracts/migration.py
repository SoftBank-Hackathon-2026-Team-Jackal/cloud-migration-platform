from typing import Literal

from pydantic import BaseModel, Field

from cmp_contracts.provisioning import ContentRef


class MigrationRequest(BaseModel):
    project_id: str = Field(min_length=1)
    source_provider: Literal["AWS"] = "AWS"
    target_provider: Literal["GCP"] = "GCP"
    # ENV-02: reference returned by `POST /env-files`; the `.env` values never travel here.
    env_file_ref: ContentRef | None = None


class MigrationCreated(BaseModel):
    workflow_id: str
    run_id: str
    mode: Literal["scaffold"] = "scaffold"


# MigrationWorkflow phase -> web `BackendPhase` (ticket 24 E17, ticket 49 F4/F9). Every
# in-progress phase is listed; anything else is a terminal phase.
WEB_PHASE: dict[str, str] = {
    "created": "analysis",
    "analysis": "analysis",
    "sandbox": "build",
    "build": "build",
    "planning_handoff": "planning",
    "planning": "planning",
    "workload_planning": "planning",
    "awaiting_approval": "awaiting_approval",
    "provisioning": "provisioning",
    "data_copy": "data_copy",
    "traffic": "traffic",
    "verification": "verification",
}
# Terminal outcomes that are failures (cancelled and scaffold_complete are not), and the web phase
# an outcome belongs to when it is not the last in-progress phase (ticket 49 F4).
FAILURE_OUTCOMES = frozenset(
    {"failed", "plan_failed", "plan_rejected", "approval_expired", "provisioning_failed"}
)
OUTCOME_PHASE: dict[str, str] = {
    "plan_failed": "planning",
    "provisioning_failed": "provisioning",
    "plan_rejected": "awaiting_approval",
    "approval_expired": "awaiting_approval",
}


class MigrationStatus(BaseModel):
    workflow_id: str
    phase: str
    approval_pending: bool
    cancel_requested: bool
    mode: Literal["scaffold"] = "scaffold"
    # Ticket 49: codes only (no messages, no secret values). `failure_phase` is a web phase.
    plan: dict[str, str | int] | None = None
    plan_code: str | None = None
    failure_code: str | None = None
    failure_phase: str | None = None
    failure_issues: list[str] = Field(default_factory=list)
    # Temporal execution status (RUNNING, COMPLETED, FAILED, TERMINATED, TIMED_OUT, CANCELED, ...)
    execution_status: str | None = None


class StepResult(BaseModel):
    step: str
    mode: Literal["scaffold"] = "scaffold"
    detail: str
