"""Plan Revision approval API (ticket 07, decisions D1-D3).

Approver identity comes only from the Bearer credential. A recorded decision, its delivery to
Temporal and the Workflow/execution state are reported as separate fields; a delivery failure is
returned as `delivery_status=FAILED`, never hidden as accepted or executed.
"""

import hmac
from typing import Annotated, Literal

from cmp_contracts.artifact import Digest
from cmp_contracts.plan import Scope
from cmp_contracts.provisioning import Text
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from cmp_api.approval_delivery import TemporalApprovalDelivery
from cmp_api.approvals import (
    ApprovalError,
    Approver,
    PlanApproval,
    authenticate_approver,
    authorize_view,
    execution_status,
    get_approval,
    mark_delivery,
    record_decision,
    register_approver,
)
from cmp_api.config import settings
from cmp_api.db import SessionLocal

router = APIRouter(prefix="/provisioning", tags=["provisioning approval"])

STATUS = {
    "APPROVER_AUTH_REQUIRED": 401,
    "APPROVER_FORBIDDEN": 403,
    "PLAN_NOT_FOUND": 404,
    "APPROVAL_NOT_FOUND": 404,
    "STALE_REVISION": 409,
    "APPROVAL_TARGET_MISMATCH": 409,
    "ALREADY_DECIDED": 409,
    "APPROVER_EXISTS": 409,
    "PLAN_NOT_APPROVABLE": 422,
}


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ApproverRegistration(Request):
    approver_id: Text
    migration_ids: list[Text] = Field(min_length=1)


class ApproverCredential(Request):
    approver_id: str
    credential: str


class ApprovalRequest(Request):
    decision: Literal["APPROVE", "REJECT"]
    scope: Scope
    manifest_digest: Digest
    preview_digest: Digest


class ApprovalResponse(Request):
    approval_id: str
    plan_id: str
    revision: int
    decision: Literal["APPROVE", "REJECT"]
    approver_id: str
    scope: Scope
    delivery_status: Literal["PENDING", "DELIVERED", "FAILED"]
    delivery_attempts: int
    execution_status: Literal["NOT_STARTED", "RUNNING", "SUCCEEDED", "FAILED"]
    workflow_state: str


def get_session():
    with SessionLocal() as session:
        yield session


def get_approval_delivery():
    return TemporalApprovalDelivery()


def require_admin(x_approval_admin_key: str | None = Header(default=None)) -> None:
    expected = settings.approval_admin_key
    if (
        not expected
        or not x_approval_admin_key
        or not hmac.compare_digest(expected, x_approval_admin_key)
    ):
        raise HTTPException(status_code=403, detail="Approver administration unavailable")


SessionDep = Annotated[Session, Depends(get_session)]
DeliveryDep = Annotated[TemporalApprovalDelivery, Depends(get_approval_delivery)]


def current_approver(
    session: SessionDep, authorization: str | None = Header(default=None)
) -> Approver:
    credential = (authorization or "").removeprefix("Bearer ")
    if not authorization or not authorization.startswith("Bearer ") or not credential:
        raise HTTPException(status_code=401, detail="APPROVER_AUTH_REQUIRED")
    try:
        return authenticate_approver(session, credential)
    except ApprovalError as exc:
        raise HTTPException(status_code=401, detail=exc.code) from None


ApproverDep = Annotated[Approver, Depends(current_approver)]


def _http_error(exc: ApprovalError) -> HTTPException:
    if exc.issues:
        detail = {"code": exc.code, "issues": exc.issues}
        return HTTPException(status_code=STATUS[exc.code], detail=detail)
    return HTTPException(status_code=STATUS.get(exc.code, 409), detail=exc.code)


async def _deliver(session: Session, delivery, record: PlanApproval, *, force=False) -> None:
    if record.delivery_status == "DELIVERED" and not force:
        return
    try:
        await delivery.deliver(
            approval_id=record.id, plan_id=record.plan_id, revision=record.revision
        )
    except Exception:  # noqa: BLE001 - boundary: record, never hide or re-raise
        mark_delivery(session, record, error="TEMPORAL_DELIVERY_FAILED")
    else:
        mark_delivery(session, record, error=None)


async def _response(session: Session, delivery, record: PlanApproval) -> ApprovalResponse:
    return ApprovalResponse(
        approval_id=record.id,
        plan_id=record.plan_id,
        revision=record.revision,
        decision=record.decision,
        approver_id=record.approver_id,
        scope=record.scope,
        delivery_status=record.delivery_status,
        delivery_attempts=record.delivery_attempts,
        execution_status=execution_status(session, record),
        workflow_state=await delivery.workflow_state(
            plan_id=record.plan_id, revision=record.revision
        ),
    )


@router.post(
    "/approvers",
    response_model=ApproverCredential,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
def create_approver(request: ApproverRegistration, session: SessionDep):
    try:
        credential = register_approver(
            session, approver_id=request.approver_id, migration_ids=request.migration_ids
        )
    except ApprovalError as exc:
        raise _http_error(exc) from None
    return ApproverCredential(approver_id=request.approver_id, credential=credential)


@router.post(
    "/plans/{plan_id}/revisions/{revision}/approvals",
    response_model=ApprovalResponse,
    status_code=201,
)
async def decide_plan(
    plan_id: str,
    revision: int,
    request: ApprovalRequest,
    response: Response,
    session: SessionDep,
    approver: ApproverDep,
    delivery: DeliveryDep,
):
    try:
        record, created = record_decision(
            session,
            approver=approver,
            plan_id=plan_id,
            revision=revision,
            decision=request.decision,
            scope=request.scope,
            manifest_digest=request.manifest_digest,
            preview_digest=request.preview_digest,
        )
    except ApprovalError as exc:
        raise _http_error(exc) from None
    if not created:
        response.status_code = 200
    await _deliver(session, delivery, record)
    return await _response(session, delivery, record)


def _visible_record(session, approver, plan_id, revision, approval_id) -> PlanApproval:
    try:
        record = get_approval(session, plan_id=plan_id, revision=revision, approval_id=approval_id)
        authorize_view(session, approver, record)
    except ApprovalError as exc:
        raise _http_error(exc) from None
    return record


@router.get(
    "/plans/{plan_id}/revisions/{revision}/approvals/{approval_id}",
    response_model=ApprovalResponse,
)
async def get_plan_approval(
    plan_id: str,
    revision: int,
    approval_id: str,
    session: SessionDep,
    approver: ApproverDep,
    delivery: DeliveryDep,
):
    record = _visible_record(session, approver, plan_id, revision, approval_id)
    return await _response(session, delivery, record)


@router.post(
    "/plans/{plan_id}/revisions/{revision}/approvals/{approval_id}/delivery",
    response_model=ApprovalResponse,
)
async def redeliver_plan_approval(
    plan_id: str,
    revision: int,
    approval_id: str,
    session: SessionDep,
    approver: ApproverDep,
    delivery: DeliveryDep,
):
    record = _visible_record(session, approver, plan_id, revision, approval_id)
    await _deliver(session, delivery, record, force=True)  # Explicit, idempotent re-send.
    return await _response(session, delivery, record)
