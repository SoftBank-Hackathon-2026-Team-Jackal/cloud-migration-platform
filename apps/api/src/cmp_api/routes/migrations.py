import asyncio
import json
from uuid import uuid4

from cmp_contracts.migration import MigrationCreated, MigrationRequest, MigrationStatus
from cmp_contracts.queues import WORKFLOW_NAME, WORKFLOW_QUEUE
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError, RPCStatusCode

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


@router.get("/{workflow_id}/events")
async def stream_migration(workflow_id: str) -> StreamingResponse:
    async def events():
        last_state = None
        while True:
            try:
                state = await (await _handle(workflow_id)).query("status")
            except RPCError:
                yield 'event: error\ndata: {"detail":"Workflow unavailable"}\n\n'
                return
            if state != last_state:
                yield f"event: status\ndata: {json.dumps(state)}\n\n"
                last_state = state
            if state["phase"] in {"scaffold_complete", "cancelled"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")
