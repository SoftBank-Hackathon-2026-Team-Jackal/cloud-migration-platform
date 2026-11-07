"""#234: 사람이 누르는 트래픽 롤백·재시도·최종 확정(웹 실제 모드 `POST /migrations/{id}/traffic/{action}`).

최종 확정(`finalize`)은 100% 전환 뒤 안정화 구간(`stabilize_seconds`)에서만 받는다.

API는 실행 중인 TrafficShiftWorkflow(`traffic-<migration_id>`, 부모 MigrationWorkflow의 자식)에 Signal만
보낸다. 되돌림은 Workflow가 기존 `traffic_rollback` Activity로 한다. 자동 재시도·자동 복귀는 없다.

- 없는 migration: 404 `MIGRATION_NOT_FOUND`
- 전환이 시작되지 않았거나 끝남, 지금 받을 수 없는 동작: 409 `TRAFFIC_NOT_RUNNING`·`TRAFFIC_ACTION_NOT_ALLOWED`
- Temporal에 닿지 못함: 503 `TRAFFIC_UNAVAILABLE`
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from temporalio.client import (
    WorkflowExecutionStatus,
    WorkflowQueryFailedError,
    WorkflowQueryRejectedError,
)
from temporalio.service import RPCError, RPCStatusCode

from cmp_api.temporal_client import get_temporal_client

router = APIRouter(prefix="/migrations", tags=["traffic actions"])

Action = Literal["rollback", "retry", "finalize", "advance"]
SIGNALS: dict[str, str] = {
    "rollback": "request_rollback",
    "retry": "request_retry",
    "finalize": "request_finalize",
    "advance": "request_advance",
}


class TrafficActionAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migration_id: str
    workflow_id: str
    action: Action
    signal: str


class TrafficUnavailable(Exception):
    """Temporal에 닿지 못했다(503)."""


class TrafficControl(Protocol):
    async def migration_exists(self, migration_id: str) -> bool: ...

    async def actions(self, workflow_id: str) -> dict[str, bool] | None:
        """실행 중인 자식의 `{rollback, retry}`. 없거나 끝났으면 None."""
        ...

    async def signal(self, workflow_id: str, name: str) -> None: ...


class TemporalTrafficControl:
    async def migration_exists(self, migration_id: str) -> bool:
        client = await get_temporal_client()
        try:
            await client.get_workflow_handle(migration_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return False
            raise TrafficUnavailable from exc
        return True

    async def actions(self, workflow_id: str) -> dict[str, bool] | None:
        handle = (await get_temporal_client()).get_workflow_handle(workflow_id)
        try:
            description = await handle.describe()
            if description.status != WorkflowExecutionStatus.RUNNING:
                return None
            status = await handle.query("status")
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return None
            raise TrafficUnavailable from exc
        except (WorkflowQueryFailedError, WorkflowQueryRejectedError) as exc:
            raise TrafficUnavailable from exc
        given = status.get("actions") if isinstance(status, dict) else None
        # 이 기능 전 Workflow(Query에 actions 없음)는 어떤 동작도 받지 않는다.
        return {key: bool((given or {}).get(key)) for key in SIGNALS}

    async def signal(self, workflow_id: str, name: str) -> None:
        handle = (await get_temporal_client()).get_workflow_handle(workflow_id)
        try:
            await handle.signal(name)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(status_code=409, detail="TRAFFIC_NOT_RUNNING") from exc
            raise TrafficUnavailable from exc


def get_traffic_control() -> TrafficControl:
    return TemporalTrafficControl()


ControlDep = Annotated[TrafficControl, Depends(get_traffic_control)]


def traffic_workflow_id(migration_id: str) -> str:
    """부모가 자식을 시작할 때 쓰는 ID 규칙(`workflows/migration.py` `_shift_traffic`)."""
    return f"traffic-{migration_id}"


async def _request(migration_id: str, action: Action, control: TrafficControl) -> dict:
    child = traffic_workflow_id(migration_id)
    try:
        if not await control.migration_exists(migration_id):
            raise HTTPException(status_code=404, detail="MIGRATION_NOT_FOUND")
        actions = await control.actions(child)
        if actions is None:
            raise HTTPException(status_code=409, detail="TRAFFIC_NOT_RUNNING")
        if not actions.get(action):
            raise HTTPException(status_code=409, detail="TRAFFIC_ACTION_NOT_ALLOWED")
        await control.signal(child, SIGNALS[action])
    except TrafficUnavailable as exc:
        raise HTTPException(status_code=503, detail="TRAFFIC_UNAVAILABLE") from exc
    return {
        "migration_id": migration_id,
        "workflow_id": child,
        "action": action,
        "signal": SIGNALS[action],
    }


ERRORS = {
    404: {"description": "MIGRATION_NOT_FOUND"},
    409: {"description": "TRAFFIC_NOT_RUNNING or TRAFFIC_ACTION_NOT_ALLOWED"},
    503: {"description": "TRAFFIC_UNAVAILABLE"},
}


@router.post(
    "/{migration_id}/traffic/rollback",
    status_code=202,
    response_model=TrafficActionAccepted,
    responses=ERRORS,
)
async def rollback_traffic(migration_id: str, control: ControlDep) -> dict:
    """전환 중(적용·관찰·판정)일 때 원본 100%로 되돌리라고 요청한다."""
    return await _request(migration_id, "rollback", control)


@router.post(
    "/{migration_id}/traffic/retry",
    status_code=202,
    response_model=TrafficActionAccepted,
    responses=ERRORS,
)
async def retry_traffic(migration_id: str, control: ControlDep) -> dict:
    """되돌린 뒤 기다리는 동안 첫 단계부터 다시 전환하라고 요청한다."""
    return await _request(migration_id, "retry", control)


@router.post(
    "/{migration_id}/traffic/advance",
    status_code=202,
    response_model=TrafficActionAccepted,
    responses=ERRORS,
)
async def advance_traffic(migration_id: str, control: ControlDep) -> dict:
    """수동 단계 모드(`manual_steps`)에서 판정을 통과하고 기다리는 중일 때 다음 전환 단계로 가라고 요청한다."""
    return await _request(migration_id, "advance", control)


@router.post(
    "/{migration_id}/traffic/finalize",
    status_code=202,
    response_model=TrafficActionAccepted,
    responses=ERRORS,
)
async def finalize_traffic(migration_id: str, control: ControlDep) -> dict:
    """100% 전환 뒤 안정화 구간에서 이전을 최종 확정하라고 요청한다."""
    return await _request(migration_id, "finalize", control)
