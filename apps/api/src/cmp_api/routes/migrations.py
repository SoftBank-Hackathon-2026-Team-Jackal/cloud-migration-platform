import asyncio
import logging
from collections.abc import Callable
from typing import Annotated, Literal
from uuid import uuid4

from cmp_contracts.events import MigrationEvent
from cmp_contracts.migration import MigrationCreated, MigrationRequest, MigrationStatus
from cmp_contracts.queues import WORKFLOW_NAME, WORKFLOW_QUEUE
from cmp_providers.env_file_store import EncryptedEnvFileStore, EnvFileError
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from temporalio.client import WorkflowHandle, WorkflowQueryFailedError, WorkflowQueryRejectedError
from temporalio.service import RPCError, RPCStatusCode

from cmp_api.approvals import ApprovalError, grant_approver_migrations
from cmp_api.config import settings
from cmp_api.connections import credential_ref
from cmp_api.db import SessionLocal
from cmp_api.event_stream import (
    StatusReader,
    StatusUnavailable,
    parse_last_event_id,
    stream_events,
)
from cmp_api.migration_events import list_events, list_logs
from cmp_api.migration_state import read_migration_state, stored_last_error
from cmp_api.routes.env_files import get_env_file_store
from cmp_api.start_request import (
    StartRefused,
    check_request,
    find_agent,
    find_connection,
    load_scope,
    load_target_profiles,
    planning_settings,
    scope_analysis_fields,
    start_input,
)
from cmp_api.temporal_client import get_temporal_client

router = APIRouter(prefix="/migrations", tags=["migration scaffold"])


async def _handle(workflow_id: str) -> WorkflowHandle:
    client = await get_temporal_client()
    return client.get_workflow_handle(workflow_id)


def get_env_file_store_factory() -> Callable[[], EncryptedEnvFileStore]:
    """Opened only when a request carries `env_file_ref`, so start without one needs no store."""
    return get_env_file_store


EnvStoreFactoryDep = Annotated[
    Callable[[], EncryptedEnvFileStore], Depends(get_env_file_store_factory)
]


def get_session_factory():
    """Opened only by a real (`target`) start, for the connection and Agent lookups."""
    return SessionLocal


SessionFactoryDep = Annotated[Callable, Depends(get_session_factory)]


logger = logging.getLogger(__name__)


def _grant_demo_approver(sessions: Callable, migration_id: str) -> None:
    """Ticket 60 A12: the web BFF holds one approver token, and each start mints a new Migration
    ID, so a configured demo approver gets this Migration (same grant as the operator API). A
    failure never blocks the start; the operator can still grant by hand."""
    approver_id = settings.demo_approver_id
    if not approver_id:
        return
    try:
        with sessions() as session:
            grant_approver_migrations(session, approver_id, [migration_id])
    except ApprovalError as exc:
        logger.warning("DEMO_APPROVER_GRANT_FAILED %s %s", exc.code, migration_id)
    except Exception:  # noqa: BLE001 — business DB unavailable: the start still proceeds
        logger.warning("DEMO_APPROVER_GRANT_FAILED DB_UNAVAILABLE %s", migration_id)


def _refused(code: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _env_ref(request: MigrationRequest, env_store: Callable[[], EncryptedEnvFileStore]):
    """ENV-02/ticket 44 S5: an object must exist as given; an `envf-` id pins its latest version."""
    store = env_store()
    try:
        if isinstance(request.env_file_ref, str):
            return store, store.latest(request.env_file_ref)
        store.check(request.env_file_ref)
    except EnvFileError as exc:
        raise _refused(exc.code) from None
    return store, request.env_file_ref


def _real_start_input(
    request: MigrationRequest,
    env_store: Callable[[], EncryptedEnvFileStore],
    sessions: Callable,
):
    """Ticket 44: `target` given → the real pipeline input (S3~S10), or a code-only refusal."""
    try:
        target, source = check_request(request)
        entry = load_target_profiles(settings.planning_target_profiles_file).get(target.profile)
        if not isinstance(entry, dict):
            raise StartRefused("TARGET_PROFILE_NOT_FOUND")
        if entry.get("provider") != target.provider:
            raise StartRefused("TARGET_PROFILE_PROVIDER_MISMATCH")
        connection = agent = None
        if target.agent_id is not None or target.credential_ref is not None:
            with sessions() as session:
                if target.agent_id is not None:
                    agent = find_agent(session, target.agent_id)
                if target.credential_ref is not None:
                    connection = find_connection(session, target.credential_ref, target.provider)
        planning = planning_settings(target, entry, connection=connection, agent=agent)
        scope_fields = None
        if request.scope_id is not None:  # 06B: filled from stored records, never from the web
            with sessions() as session:
                scope, source_connection = load_scope(session, request.scope_id)
                scope_fields = scope_analysis_fields(scope, credential_ref(source_connection))
    except StartRefused as exc:
        raise _refused(exc.code, exc.status) from None
    store, env_ref = _env_ref(request, env_store)
    return start_input(request, source, planning, env_ref, scope_fields), store, env_ref


@router.post("", response_model=MigrationCreated, status_code=202)
async def start_migration(
    request: MigrationRequest, env_store: EnvStoreFactoryDep, sessions: SessionFactoryDep
) -> MigrationCreated:
    """Start a migration. Unknown fields are 422.

    Without `target` this is the scaffold start: `env_file_ref` (from `POST /env-files`) is
    optional. With `target` (the web `StartRequest`, ticket 44) the real pipeline starts: `source`
    and `env_file_ref` (an `envf-` id, pinned to its latest version, or an exact reference) are
    required and the target profile catalog supplies the target settings. Refusals are
    `{"detail": {"code": ...}}`: 422 `SOURCE_REQUIRED`, `ENV_FILE_REF_REQUIRED`,
    `ENV_FILE_NOT_FOUND` | `ENV_FILE_DIGEST_MISMATCH` | `ENV_FILE_EXPIRED`, `TARGET_*`; 503
    `TARGET_PROFILES_UNCONFIGURED` or the `.env` store codes. Nothing starts on a refusal."""
    store = env_ref = None
    if request.scope_id is not None and request.target is None:
        raise _refused("SCOPE_NOT_APPLICABLE")  # 06B: a scope belongs to a real start
    if request.target is not None:
        data, store, env_ref = _real_start_input(request, env_store, sessions)
    elif request.env_file_ref is not None:
        # ENV-02: only an existing version with the same sha256 may start a migration.
        store, env_ref = _env_ref(request, env_store)
        data = {"project_id": request.project_id, "env_file_ref": env_ref.model_dump(mode="json")}
    else:
        data = request.project_id  # the scaffold string input, as before
    client = await get_temporal_client()
    workflow_id = f"migration-{uuid4()}"
    _grant_demo_approver(sessions, workflow_id)  # before planning can ask for approval
    handle = await client.start_workflow(
        WORKFLOW_NAME,
        data,
        id=workflow_id,
        task_queue=WORKFLOW_QUEUE,
    )
    if store is not None:
        store.bind_migration(env_ref, workflow_id)  # retention follows this run
    return MigrationCreated(workflow_id=workflow_id, run_id=handle.result_run_id)


@router.get("/{workflow_id}", response_model=MigrationStatus)
async def get_migration(workflow_id: str) -> MigrationStatus:
    """Ticket 49: a closed Workflow reports its Temporal outcome (never `running`), with the
    failure code, web phase and detailed codes (`failure_issues`)."""
    try:
        state = await read_migration_state(
            await _handle(workflow_id), last_error=stored_last_error(SessionLocal)
        )
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(status_code=code, detail="Workflow not found or unavailable") from exc
    return MigrationStatus(workflow_id=workflow_id, **state)


@router.post(
    "/{workflow_id}/approve",
    status_code=202,
    responses={
        409: {"description": "`gate=cutover`: DB is not ready to switch (`CUTOVER_NOT_READY`)"}
    },
)
async def approve_migration(
    workflow_id: str, gate: Annotated[Literal["cutover"] | None, Query()] = None
) -> dict[str, str]:
    """Without `gate`: the scaffold approval Signal. `gate=cutover` (web second approval): switch
    the database of this migration (child `data-<id>`, specs/data-migration). Accepted only while
    the source is the writer and replication is streaming; otherwise 409 `CUTOVER_NOT_READY`."""
    if gate == "cutover":
        return await _approve_cutover(workflow_id)
    try:
        await (await _handle(workflow_id)).signal("approve")
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(status_code=code, detail="Workflow not found or unavailable") from exc
    return {"workflow_id": workflow_id, "signal": "approve"}


async def _approve_cutover(workflow_id: str) -> dict[str, str]:
    child = f"data-{workflow_id}"
    try:
        handle = await _handle(child)
        status = await handle.query("status")
        if status.get("phase") != "streaming" or status.get("active") != "source":
            raise HTTPException(
                status_code=409,
                detail={"code": "CUTOVER_NOT_READY", "phase": status.get("phase")},
            )
        await handle.signal("switchover")
    except (WorkflowQueryFailedError, WorkflowQueryRejectedError) as exc:
        raise HTTPException(status_code=503, detail="Data migration unavailable") from exc
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(
            status_code=code, detail="Data migration not found or unavailable"
        ) from exc
    return {"workflow_id": child, "signal": "switchover"}


@router.post("/{workflow_id}/cancel", status_code=202)
async def cancel_migration(workflow_id: str) -> dict[str, str]:
    try:
        await (await _handle(workflow_id)).signal("request_cancel")
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(status_code=code, detail="Workflow not found or unavailable") from exc
    return {"workflow_id": workflow_id, "signal": "request_cancel"}


async def read_workflow_status(workflow_id: str) -> dict | None:
    """MigrationWorkflow state (ticket 49: execution status applied); None when not found."""
    try:
        return await read_migration_state(
            await _handle(workflow_id), last_error=stored_last_error(SessionLocal)
        )
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
