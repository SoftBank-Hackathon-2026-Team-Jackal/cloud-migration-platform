"""Operator Agent job control (ticket 52, D5/D7): show, cancel and request a teardown.

Same administrator key as Agent enrollment/revocation (ticket 15). Responses carry codes and
identifiers only.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from cmp_api.agent_jobs import AgentJobError, cancel_job, create_teardown, job_view
from cmp_api.agent_registry import AgentJob
from cmp_api.db import SessionLocal
from cmp_api.routes.agents import require_admin

router = APIRouter(
    prefix="/agent-admin/jobs",
    tags=["on-premises agents"],
    dependencies=[Depends(require_admin)],
)


class AgentJobView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    mode: str | None
    agent_id: str
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN", "EXPIRED", "CANCELLED"]
    code: str | None
    cancel_requested: bool


class TeardownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # True also deletes the database PVC and the Namespace (irreversible, D8).
    delete_data: bool = False


def get_session():
    with SessionLocal() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
CONFLICT = {409: {"description": "Code only, e.g. JOB_ALREADY_FINISHED"}}


@router.get("/{job_id}", response_model=AgentJobView, responses={404: {}})
def show_job(job_id: str, session: SessionDep):
    job = session.get(AgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    return job_view(job)


@router.post("/{job_id}/cancel", response_model=AgentJobView, responses={404: {}, **CONFLICT})
def cancel(job_id: str, session: SessionDep):
    """QUEUED → CANCELLED (never runs). RUNNING → cancel requested: downloads stop and a reclaim
    closes it as UNKNOWN; a Helm step already in progress finishes and is reported."""
    try:
        return job_view(cancel_job(session, job_id))
    except AgentJobError as exc:
        status = 404 if exc.code == "JOB_NOT_FOUND" else 409
        raise HTTPException(status_code=status, detail=exc.code) from None


@router.post("/{job_id}/teardown", response_model=AgentJobView, status_code=201, responses=CONFLICT)
def teardown(job_id: str, request: TeardownRequest, session: SessionDep):
    """Queue the removal of what rollout job `job_id` deployed (Helm Releases, platform
    Secrets/NetworkPolicies; with `delete_data` also the DB PVC and the Namespace)."""
    try:
        return job_view(create_teardown(session, job_id, delete_data=request.delete_data))
    except AgentJobError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None
