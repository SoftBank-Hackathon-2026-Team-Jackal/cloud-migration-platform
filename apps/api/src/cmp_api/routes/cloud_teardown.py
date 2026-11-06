"""Operator cloud teardown (ticket 53, D1·D2·D5): start, show and confirm.

Same administrator key as Agent job control (52). Starting removes workloads and load balancers
and prepares a destroy review; nothing is destroyed until the review Digest is confirmed.
Responses carry codes, counts, resource addresses and non-secret IDs only.
"""

from typing import Annotated, Any, Literal

from cmp_contracts.queues import WORKFLOW_QUEUE
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from cmp_api.cloud_teardown import (
    TEARDOWN_PREFIX,
    TeardownError,
    check_teardown_target,
    teardown_id,
)
from cmp_api.db import SessionLocal
from cmp_api.routes.agents import require_admin
from cmp_api.temporal_client import get_temporal_client

WORKFLOW_NAME = "CloudTeardownWorkflow"
router = APIRouter(
    prefix="/provisioning-admin/teardowns",
    tags=["provisioning administration"],
    dependencies=[Depends(require_admin)],
)


class CloudTeardownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    revision: int = Field(ge=1)
    # True also deletes the platform Namespaces' database disks (irreversible, D4).
    delete_data: bool = False


class CloudTeardownConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CloudTeardownView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teardown_id: str
    phase: Literal["WORKLOADS", "DESTROY_PLAN", "AWAITING_CONFIRMATION", "DESTROYING", "FINISHED"]
    delete_data: bool = False
    workloads: dict[str, Any] | None = None
    review: dict[str, Any] | None = None  # {digest, addresses, unprotect}
    result: dict[str, Any] | None = None


def get_session():
    with SessionLocal() as session:
        yield session


async def get_client() -> Client:
    return await get_temporal_client()


SessionDep = Annotated[Session, Depends(get_session)]
ClientDep = Annotated[Client, Depends(get_client)]
CONFLICT = {409: {"description": "Code only, e.g. TEARDOWN_IN_PROGRESS"}}


async def _status(client: Client, workflow_id: str) -> CloudTeardownView:
    if not workflow_id.startswith(TEARDOWN_PREFIX):
        raise HTTPException(status_code=404, detail="TEARDOWN_NOT_FOUND")
    try:
        status = await client.get_workflow_handle(workflow_id).query("status")
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(status_code=code, detail="TEARDOWN_NOT_FOUND") from None
    return CloudTeardownView(teardown_id=workflow_id, **status)


@router.post("", response_model=CloudTeardownView, status_code=202, responses={404: {}, **CONFLICT})
async def start(request: CloudTeardownRequest, session: SessionDep, client: ClientDep):
    """Start tearing down what Revision `plan_id`/`revision` created (workloads first)."""
    try:
        check_teardown_target(session, request.plan_id, request.revision)
    except TeardownError as exc:
        status = 404 if exc.code == "PLAN_NOT_FOUND" else 409
        raise HTTPException(status_code=status, detail=exc.code) from None
    workflow_id = teardown_id(request.plan_id, request.revision)
    try:
        await client.start_workflow(
            WORKFLOW_NAME,
            request.model_dump(),
            id=workflow_id,
            task_queue=WORKFLOW_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        raise HTTPException(status_code=409, detail="TEARDOWN_IN_PROGRESS") from None
    return CloudTeardownView(
        teardown_id=workflow_id, phase="WORKLOADS", delete_data=request.delete_data
    )


@router.get("/{teardown_id}", response_model=CloudTeardownView, responses={404: {}})
async def show(teardown_id: str, client: ClientDep):
    return await _status(client, teardown_id)


@router.post(
    "/{teardown_id}/confirm", response_model=CloudTeardownView, status_code=202,
    responses={404: {}, **CONFLICT},
)  # fmt: skip
async def confirm(teardown_id: str, request: CloudTeardownConfirm, client: ClientDep):
    """Destroy exactly the reviewed addresses (`review.digest`); anything else is refused."""
    view = await _status(client, teardown_id)
    if view.phase != "AWAITING_CONFIRMATION":
        raise HTTPException(status_code=409, detail="NOT_AWAITING_CONFIRMATION")
    if (view.review or {}).get("digest") != request.review_digest:
        raise HTTPException(status_code=409, detail="DESTROY_REVIEW_MISMATCH")
    await client.get_workflow_handle(teardown_id).signal("confirm_destroy", request.review_digest)
    return view.model_copy(update={"phase": "DESTROYING"})
