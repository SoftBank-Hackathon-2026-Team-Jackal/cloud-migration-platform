"""Read-only approved ON_PREM rollout status for approvers (ticket 12, E6).

Reports the Agent job state of one Plan Revision. It never starts, retries or cancels work.
"""

from typing import Annotated

from cmp_contracts.onprem_rollout import RolloutState
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from cmp_api.approvals import (
    ApprovalError,
    Approver,
    PlanRevision,
    authenticate_approver,
)
from cmp_api.db import SessionLocal
from cmp_api.onprem_rollout import rollout_status

router = APIRouter(prefix="/provisioning", tags=["provisioning on-premises rollout"])


class RolloutStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    revision: int
    state: RolloutState
    code: str | None
    job_id: str | None
    agent_id: str | None


def get_session():
    with SessionLocal() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def rollout_viewer(
    session: SessionDep, authorization: str | None = Header(default=None)
) -> Approver:
    credential = (authorization or "").removeprefix("Bearer ")
    if not authorization or not authorization.startswith("Bearer ") or not credential:
        raise HTTPException(status_code=401, detail="APPROVER_AUTH_REQUIRED")
    try:
        return authenticate_approver(session, credential)
    except ApprovalError:
        raise HTTPException(status_code=401, detail="APPROVER_AUTH_REQUIRED") from None


@router.get("/plans/{plan_id}/revisions/{revision}/rollout", response_model=RolloutStatusResponse)
def get_rollout(
    plan_id: str,
    revision: int,
    session: SessionDep,
    approver: Annotated[Approver, Depends(rollout_viewer)],
):
    row = session.get(PlanRevision, (plan_id, revision))
    if row is None:
        raise HTTPException(status_code=404, detail="PLAN_NOT_FOUND")
    if row.migration_id not in approver.migration_ids:
        raise HTTPException(status_code=403, detail="APPROVER_FORBIDDEN")
    return {"plan_id": plan_id, "revision": revision, **rollout_status(session, plan_id, revision)}
