import asyncio
from collections.abc import Callable
from typing import Annotated
from uuid import uuid4

from cmp_contracts.events import MigrationEvent
from cmp_contracts.migration import MigrationCreated, MigrationRequest, MigrationStatus
from cmp_contracts.queues import WORKFLOW_NAME, WORKFLOW_QUEUE
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError, RPCStatusCode

from cmp_api.db import SessionLocal
from cmp_api.event_stream import (
    StatusReader,
    StatusUnavailable,
    parse_last_event_id,
    stream_events,
)
from cmp_api.migration_events import list_events, list_logs
from cmp_api.temporal_client import get_temporal_client

router = APIRouter(prefix="/migrations", tags=["migration scaffold"])


async def _handle(workflow_id: str) -> WorkflowHandle:
    client = await get_temporal_client()
    return client.get_workflow_handle(workflow_id)


@router.post("", response_model=MigrationCreated, status_code=202)
async def start_migration(request: MigrationRequest) -> MigrationCreated:
    client = await get_temporal_client()
    workflow_id = f"migration-{uuid4()}"
    handle = await client.start_workflow(
        WORKFLOW_NAME,
        request.project_id,
        id=workflow_id,
        task_queue=WORKFLOW_QUEUE,
    )
    return MigrationCreated(workflow_id=workflow_id, run_id=handle.result_run_id)


@router.get("/{workflow_id}", response_model=MigrationStatus)
async def get_migration(workflow_id: str) -> MigrationStatus:
    try:
        state = await (await _handle(workflow_id)).query("status")
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(status_code=code, detail="Workflow not found or unavailable") from exc
    return MigrationStatus(workflow_id=workflow_id, **state)


@router.post("/{workflow_id}/approve", status_code=202)
async def approve_migration(workflow_id: str) -> dict[str, str]:
    try:
        await (await _handle(workflow_id)).signal("approve")
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(status_code=code, detail="Workflow not found or unavailable") from exc
    return {"workflow_id": workflow_id, "signal": "approve"}


@router.post("/{workflow_id}/cancel", status_code=202)
async def cancel_migration(workflow_id: str) -> dict[str, str]:
    try:
        await (await _handle(workflow_id)).signal("request_cancel")
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(status_code=code, detail="Workflow not found or unavailable") from exc
    return {"workflow_id": workflow_id, "signal": "request_cancel"}


async def read_workflow_status(workflow_id: str) -> dict | None:
    """MigrationWorkflow `status` query; None when the Workflow does not exist."""
    try:
        return await (await _handle(workflow_id)).query("status")
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return None
        raise StatusUnavailable from None
    except Exception:  # noqa: BLE001 - no worker/Temporal: fall back to stored events
        raise StatusUnavailable from None


def get_status_reader() -> StatusReader:
    return read_workflow_status


def get_event_sessions():
    return SessionLocal


class MigrationEventsPage(BaseModel):
    migration_id: str
    events: list[MigrationEvent]
    next_after: int


class MigrationLogsPage(BaseModel):
    migration_id: str
    logs: list[dict]
    next_after: int


StatusDep = Annotated[StatusReader, Depends(get_status_reader)]
SessionsDep = Annotated[Callable, Depends(get_event_sessions)]


async def _initial_state(read_status: StatusReader, workflow_id: str) -> tuple[dict | None, bool]:
    """(state, exists): `exists` is False only when Temporal says the Workflow is not found."""
    try:
        state = await read_status(workflow_id)
    except StatusUnavailable:
        return None, True
    return state, state is not None


def _has_events(sessions, workflow_id: str) -> bool:
    with sessions() as session:
        return bool(list_events(session, workflow_id, limit=1))


@router.get(
    "/{workflow_id}/events",
    response_model=None,
    responses={
        200: {
            "model": MigrationEventsPage,
            "description": "SSE (`text/event-stream`, default) or JSON page (`?after=`).",
            "content": {"text/event-stream": {}},
        },
        404: {"description": "Workflow not found and no stored events"},
    },
)
async def stream_migration(
    workflow_id: str,
    sessions: SessionsDep,
    read_status: StatusDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    legacy: bool = True,
    accept: Annotated[str | None, Header(include_in_schema=False)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
):
    """Ticket 24 (E10-E13): SSE with snapshot/`Last-Event-ID`/heartbeat, or a JSON page."""
    state, exists = await _initial_state(read_status, workflow_id)
    if not exists and not await asyncio.to_thread(_has_events, sessions, workflow_id):
        raise HTTPException(status_code=404, detail="Migration not found")
    wants_json = after is not None or (
        accept is not None and "application/json" in accept and "text/event-stream" not in accept
    )
    if wants_json:
        with sessions() as session:
            events = list_events(session, workflow_id, after=after or 0, limit=limit)
        return MigrationEventsPage(
            migration_id=workflow_id,
            events=events,
            next_after=events[-1].seq if events else (after or 0),
        )
    body = stream_events(
        workflow_id,
        sessions=sessions,
        read_status=read_status,
        last_event_id=parse_last_event_id(last_event_id),
        legacy=legacy,
        initial_state=state,
    )
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{workflow_id}/logs", response_model=MigrationLogsPage)
def migration_logs(
    workflow_id: str,
    sessions: SessionsDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> MigrationLogsPage:
    """Ticket 24 (E14): masked operational log for the agent and the final report."""
    with sessions() as session:
        logs = list_logs(session, workflow_id, after=after, limit=limit)
    return MigrationLogsPage(
        migration_id=workflow_id, logs=logs, next_after=logs[-1]["id"] if logs else after
    )
