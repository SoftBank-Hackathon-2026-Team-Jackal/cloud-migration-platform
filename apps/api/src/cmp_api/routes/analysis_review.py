"""분석 검토 게이트 API: 필수 기능 확정과 손실·보안 승인(웹 실제 모드).

분석 자식 Workflow(`analysis-<migration_id>`)는 매핑 뒤 사람의 결정이 올 때까지 멈춘다(결정이 없으면
영원히 기다리고, 자동 승인은 없다). 이 API는 그 Workflow의 Query(`review_state`)를 읽고, 지금 그 결정을
기다릴 때만 Signal(`confirm_requirements`·`approve_losses`)을 보낸다. 판단과 검증은 Workflow의 상태
기계(`ReviewGate`)가 하고, 여기서는 얇게 전달한다. CLI `scripts/review_signal.py`와 같은 규칙이다.

- `GET  /migrations/{id}/analysis/review`          지금 상태와 검토 항목(기능·등급·잃는 것·보안)
- `POST /migrations/{id}/analysis/review/confirm`  모든 행의 필수 여부를 확정
- `POST /migrations/{id}/analysis/review/approve`  승인이 필요한 모든 항목을 승인·거절

오류(모두 `{"detail": <코드>}`): 404 `MIGRATION_NOT_FOUND`·`ANALYSIS_NOT_FOUND`, 409
`REVIEW_NOT_PENDING`·`ANALYSIS_REF_MISMATCH`, 422 거절 사유(`UNKNOWN_ROW`·`MISSING_ROW`·
`NOT_APPROVABLE`·`INVALID_PAYLOAD`), 503 `REVIEW_UNAVAILABLE`.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from temporalio.client import WorkflowQueryFailedError, WorkflowQueryRejectedError
from temporalio.service import RPCError, RPCStatusCode

from cmp_api.temporal_client import get_temporal_client

router = APIRouter(prefix="/migrations", tags=["analysis review"])

CONFIRM = "confirm_requirements"
APPROVE = "approve_losses"
DEFAULT_ACTOR = "web-user"
# Signal은 응답 없이 전달되므로, 보낸 뒤 Workflow가 받아들였는지(단계가 넘어갔는지)·거절했는지를 잠깐 본다.
SETTLE_TRIES = 12
SETTLE_SECONDS = 0.25


class ReviewUnavailable(Exception):
    """Temporal에 닿지 못했다(503)."""


class ReviewControl(Protocol):
    async def migration_exists(self, migration_id: str) -> bool: ...

    async def state(self, migration_id: str) -> dict | None:
        """분석 자식의 `review_state`. 자식이 없으면 None."""
        ...

    async def signal(self, migration_id: str, name: str, payload: dict) -> None: ...


def analysis_workflow_id(migration_id: str) -> str:
    """부모가 분석 자식을 시작할 때 쓰는 ID 규칙(`workflows/migration.py`, `review_client.child_id`)."""
    return f"analysis-{migration_id}"


class TemporalReviewControl:
    async def migration_exists(self, migration_id: str) -> bool:
        client = await get_temporal_client()
        try:
            await client.get_workflow_handle(migration_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return False
            raise ReviewUnavailable from exc
        return True

    async def state(self, migration_id: str) -> dict | None:
        handle = (await get_temporal_client()).get_workflow_handle(
            analysis_workflow_id(migration_id)
        )
        try:
            return await handle.query("review_state")
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return None
            raise ReviewUnavailable from exc
        except (WorkflowQueryFailedError, WorkflowQueryRejectedError) as exc:
            raise ReviewUnavailable from exc

    async def signal(self, migration_id: str, name: str, payload: dict) -> None:
        handle = (await get_temporal_client()).get_workflow_handle(
            analysis_workflow_id(migration_id)
        )
        try:
            await handle.signal(name, payload)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(status_code=404, detail="ANALYSIS_NOT_FOUND") from exc
            raise ReviewUnavailable from exc


def get_review_control() -> ReviewControl:
    return TemporalReviewControl()


ControlDep = Annotated[ReviewControl, Depends(get_review_control)]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ConfirmRequest(Request):
    """모든 매핑 행의 필수 여부(행 ID → true/false). 빠진 행이나 모르는 행은 거절된다. 행이 없으면(보안 승인만 남은 경우) 빈 객체."""

    analysis_version: str = Field(min_length=1, max_length=64)
    required: dict[str, bool]  # may be empty: no mapping rows, only a security approval is left
    operational: list[str] = Field(default_factory=list)
    actor: str | None = Field(default=None, min_length=1, max_length=100)


class ApproveRequest(Request):
    """승인이 필요한 모든 항목(행 ID·`security:<기준>`)의 결정. 거절하면 그 기능은 필수가 된다."""

    analysis_version: str = Field(min_length=1, max_length=64)
    approvals: dict[str, Literal["APPROVED", "REJECTED"]] = Field(min_length=1)
    actor: str | None = Field(default=None, min_length=1, max_length=100)


class ReviewView(BaseModel):
    """`ReviewGate.view()` 그대로(웹이 보여줄 값). 항목 모양은 `cmp_worker.review_gate`가 정한다."""

    phase: str
    analysis_ref: dict[str, Any] | None
    pending: Literal["confirm_requirements", "approve_losses"] | None
    items: list[dict[str, Any]]
    security: list[dict[str, Any]]
    blocked: list[Any]
    rejections: list[dict[str, Any]]


class ReviewAccepted(BaseModel):
    migration_id: str
    signal: str
    phase: str
    pending: str | None


ERRORS = {
    404: {"description": "MIGRATION_NOT_FOUND or ANALYSIS_NOT_FOUND"},
    409: {"description": "REVIEW_NOT_PENDING or ANALYSIS_REF_MISMATCH"},
    422: {"description": "The review gate refused the decision (code in `detail`)"},
    503: {"description": "REVIEW_UNAVAILABLE"},
}


async def _state(migration_id: str, control: ReviewControl) -> dict:
    if not await control.migration_exists(migration_id):
        raise HTTPException(status_code=404, detail="MIGRATION_NOT_FOUND")
    state = await control.state(migration_id)
    if state is None:
        raise HTTPException(status_code=404, detail="ANALYSIS_NOT_FOUND")
    return state


async def _decide(
    migration_id: str, name: str, version: str, fields: dict, control: ReviewControl
) -> dict:
    try:
        state = await _state(migration_id, control)
        if state.get("pending") != name:
            raise HTTPException(status_code=409, detail="REVIEW_NOT_PENDING")
        ref = state.get("analysis_ref") or {}
        if str(ref.get("version")) != version:
            raise HTTPException(status_code=409, detail="ANALYSIS_REF_MISMATCH")
        before = len(state.get("rejections") or [])
        await control.signal(
            migration_id,
            name,
            {"analysis_ref": {"id": ref["id"], "version": ref["version"]}, **fields},
        )
        for _ in range(SETTLE_TRIES):
            now = await control.state(migration_id) or {}
            rejections = now.get("rejections") or []
            if len(rejections) > before:  # the gate refused it: say why instead of "accepted"
                raise HTTPException(
                    status_code=422, detail=str(rejections[-1].get("code") or "REJECTED")
                )
            if now.get("pending") != name:  # the gate moved on: accepted
                return {
                    "migration_id": migration_id,
                    "signal": name,
                    "phase": now.get("phase", ""),
                    "pending": now.get("pending"),
                }
            await asyncio.sleep(SETTLE_SECONDS)
    except ReviewUnavailable as exc:
        raise HTTPException(status_code=503, detail="REVIEW_UNAVAILABLE") from exc
    # 보냈지만 아직 반영을 못 봤다. Signal은 이미 전달됐으므로 다시 보내지 말고 상태를 다시 읽게 한다.
    return {"migration_id": migration_id, "signal": name, "phase": "PENDING", "pending": name}


@router.get(
    "/{migration_id}/analysis/review",
    response_model=ReviewView,
    responses=ERRORS,
)
async def get_review(migration_id: str, control: ControlDep) -> dict:
    """분석이 사람의 결정을 기다리는지(`pending`)와 검토할 항목. 기다리지 않으면 `pending`은 null."""
    try:
        state = await _state(migration_id, control)
    except ReviewUnavailable as exc:
        raise HTTPException(status_code=503, detail="REVIEW_UNAVAILABLE") from exc
    return state


@router.post(
    "/{migration_id}/analysis/review/confirm",
    status_code=202,
    response_model=ReviewAccepted,
    responses=ERRORS,
)
async def confirm_requirements(
    migration_id: str, request: ConfirmRequest, control: ControlDep
) -> dict:
    """필수 기능을 확정한다. 모든 행이 있어야 하고, 확정 뒤 잃는 것이 있으면 승인 단계로 넘어간다."""
    fields = {
        "required": request.required,
        "operational": request.operational,
        "actor": request.actor or DEFAULT_ACTOR,
    }
    return await _decide(migration_id, CONFIRM, request.analysis_version, fields, control)


@router.post(
    "/{migration_id}/analysis/review/approve",
    status_code=202,
    response_model=ReviewAccepted,
    responses=ERRORS,
)
async def approve_losses(migration_id: str, request: ApproveRequest, control: ControlDep) -> dict:
    """잃는 것과 보안 낮아짐을 승인·거절한다. 승인이 필요한 항목 전부의 결정이 있어야 한다."""
    fields = {"approvals": request.approvals, "actor": request.actor or DEFAULT_ACTOR}
    return await _decide(migration_id, APPROVE, request.analysis_version, fields, control)
