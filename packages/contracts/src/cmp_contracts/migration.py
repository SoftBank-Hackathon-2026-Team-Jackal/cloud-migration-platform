from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from cmp_contracts.provisioning import ContentRef

TargetProvider = Literal["AWS", "GCP", "ON_PREM"]
# Ticket 44 (S5): the web sends the upload `id` only; the API pins its latest version at start.
EnvFileId = Annotated[str, Field(pattern=r"^envf-[a-f0-9]{32}$")]


class StartTarget(BaseModel):
    """Web `StartRequest.target` (cloud-migration-web `contract/rest.schema.json`), plus AWS."""

    model_config = ConfigDict(extra="forbid")

    provider: TargetProvider
    profile: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    region: str | None = Field(default=None, min_length=1, max_length=63)
    agent_id: str | None = Field(default=None, min_length=1, max_length=120)
    # Target connection: its ID `conn-<hex>` or its AWS profile name `cmp-conn-<hex>` (ticket 23).
    credential_ref: str | None = Field(default=None, min_length=1, max_length=64)


class StartSource(BaseModel):
    """Web `StartRequest.source`: a public GitHub repository pinned to a commit."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["GITHUB_PUBLIC"]
    repository_id: int = Field(ge=1)
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class MigrationRequest(BaseModel):
    """Ticket 44: without `target` this is the scaffold start (unchanged); with it the real
    pipeline starts and `source` and `env_file_ref` are required. Unknown fields are refused."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    source_provider: Literal["AWS"] = "AWS"
    # Legacy top-level copy of `target.provider`; when both are given they must be equal.
    target_provider: TargetProvider | None = None
    target: StartTarget | None = None
    source: StartSource | None = None
    # ENV-02: reference returned by `POST /env-files`; the `.env` values never travel here.
    env_file_ref: ContentRef | EnvFileId | None = None


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
    {
        "failed",
        "plan_failed",
        "plan_rejected",
        "approval_expired",
        "provisioning_failed",
        "data_blocked",
        "data_failed",
    }
)
OUTCOME_PHASE: dict[str, str] = {
    "plan_failed": "planning",
    "provisioning_failed": "provisioning",
    "plan_rejected": "awaiting_approval",
    "approval_expired": "awaiting_approval",
    "data_blocked": "data_copy",  # specs/data-migration: handoff/precheck blocked the copy
    "data_failed": "data_copy",
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
    # specs/data-migration: DataMigrationWorkflow state {phase, active, failure_code, switches}
    data_migration: dict[str, object] | None = None
    failure_phase: str | None = None
    failure_issues: list[str] = Field(default_factory=list)
    # Temporal execution status (RUNNING, COMPLETED, FAILED, TERMINATED, TIMED_OUT, CANCELED, ...)
    execution_status: str | None = None


class StepResult(BaseModel):
    step: str
    mode: Literal["scaffold"] = "scaffold"
    detail: str
