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


class MigrationStatus(BaseModel):
    workflow_id: str
    phase: str
    approval_pending: bool
    cancel_requested: bool
    mode: Literal["scaffold"] = "scaffold"


class StepResult(BaseModel):
    step: str
    mode: Literal["scaffold"] = "scaffold"
    detail: str
