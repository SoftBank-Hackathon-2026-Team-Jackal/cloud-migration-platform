"""Planning input registration and approver Preview views (ticket 18, P1/P6).

Preview views use the 07 approver credential and Migration grant. The response carries the
stored Preview, which 05/06/12A build without Secret values, and the Digest to approve with.
"""

from pathlib import Path
from typing import Annotated, Literal

from cmp_contracts.plan import PlanManifest, Preview, Scope
from cmp_contracts.plan_publish import (
    PlanningInputRegistration,
    plan_id_for,
    workload_plan_id_for,
)
from cmp_providers.artifacts import sha256_json
from cmp_providers.plans import approval_issues, manifest_digest
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cmp_api.approvals import PlanRevision, latest_revision
from cmp_api.config import settings
from cmp_api.db import SessionLocal
from cmp_api.onprem_rollout import LocalArtifactStore
from cmp_api.plan_inputs import PlanInputError, register_planning_input
from cmp_api.routes.approvals import ApproverDep, require_admin

router = APIRouter(prefix="/provisioning", tags=["provisioning planning"])


def get_session():
    with SessionLocal() as session:
        yield session


def get_artifact_store() -> LocalArtifactStore:
    if not settings.artifact_store_dir:
        raise HTTPException(status_code=503, detail="ARTIFACT_STORE_NOT_CONFIGURED")
    return LocalArtifactStore(Path(settings.artifact_store_dir))


SessionDep = Annotated[Session, Depends(get_session)]
StoreDep = Annotated[LocalArtifactStore, Depends(get_artifact_store)]


class PlanningInputRegistered(BaseModel):
    migration_id: str
    input_digest: str


class PlanPreviewView(BaseModel):
    plan_id: str
    revision: int
    latest: bool
    migration_id: str
    provider: Literal["AWS", "GCP", "ON_PREM"]
    scope: Scope
    manifest_digest: str
    preview_digest: str
    approvable: bool
    approval_issues: list[str]
    preview: Preview


@router.put(
    "/migrations/{migration_id}/planning-input",
    response_model=PlanningInputRegistered,
    dependencies=[Depends(require_admin)],
)
def put_planning_input(
    migration_id: str,
    registration: PlanningInputRegistration,
    session: SessionDep,
    store: StoreDep,
):
    try:
        return register_planning_input(session, migration_id, registration, store)
    except PlanInputError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code}) from None


def _view(session: Session, approver, row: PlanRevision | None) -> PlanPreviewView:
    if row is None:
        raise HTTPException(status_code=404, detail="PLAN_NOT_FOUND")
    if row.migration_id not in approver.migration_ids:
        raise HTTPException(status_code=403, detail="APPROVER_FORBIDDEN")
    manifest = PlanManifest.model_validate(row.manifest)
    preview = Preview.model_validate(row.preview)
    issues = sorted({issue.code for issue in approval_issues(manifest, preview)})
    return PlanPreviewView(
        plan_id=row.plan_id,
        revision=row.revision,
        latest=row.revision == latest_revision(session, row.plan_id),
        migration_id=row.migration_id,
        provider=manifest.target.provider,
        scope=manifest.scope,
        manifest_digest=manifest_digest(manifest),
        preview_digest=sha256_json(preview.model_dump(mode="json")),
        approvable=not issues,
        approval_issues=issues,
        preview=preview,
    )


@router.get(
    "/plans/{plan_id}/revisions/{revision}/preview",
    response_model=PlanPreviewView,
)
def get_plan_preview(plan_id: str, revision: int, session: SessionDep, approver: ApproverDep):
    return _view(session, approver, session.get(PlanRevision, (plan_id, revision)))


@router.get("/migrations/{migration_id}/plan", response_model=PlanPreviewView)
def get_migration_plan(migration_id: str, session: SessionDep, approver: ApproverDep):
    """Latest Revision of the Plan to approve now (18 P6, 22 W10).

    AWS/GCP: the WORKLOADS Plan once it is published, else the infrastructure Plan.
    """
    row = None
    for plan_id in (workload_plan_id_for(migration_id), plan_id_for(migration_id)):
        revision = latest_revision(session, plan_id)
        if revision:
            row = session.get(PlanRevision, (plan_id, revision))
            break
    return _view(session, approver, row)
