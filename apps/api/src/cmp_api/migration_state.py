"""MigrationWorkflow state for the status API and SSE (ticket 49 F8, follow-up D-19).

The `status` query only knows what Workflow code recorded. A Workflow that was terminated, timed
out or failed outside its own code keeps answering its last in-progress phase, so the Temporal
execution status decides first: a closed failure is never reported as `running`.

A closed run is queried by replaying it on a worker. When no worker answers (stopped, deploying),
the outcome comes from what Temporal and the event store already hold (D-19): the Workflow result
(a closed run returns it at once) and the last stored `error` event (code and web phase).
"""

import asyncio
from collections.abc import Callable
from datetime import timedelta

from cmp_contracts.migration import FAILURE_OUTCOMES, OUTCOME_PHASE, WEB_PHASE
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus, WorkflowHandle

QUERY_TIMEOUT_SECONDS = 5.0  # queries need a worker; none may be running

# migration_id -> stored envelope of the last `error` event ({phase, data: {code, phase}}), or None
LastError = Callable[[str], dict | None]

_FAILED = {
    WorkflowExecutionStatus.FAILED: "WORKFLOW_FAILED",
    WorkflowExecutionStatus.TERMINATED: "WORKFLOW_TERMINATED",
    WorkflowExecutionStatus.TIMED_OUT: "WORKFLOW_TIMED_OUT",
}


async def read_migration_state(
    handle: WorkflowHandle, *, last_error: LastError | None = None
) -> dict:
    """Query state with the execution status applied. `RPCError` (NOT_FOUND, or a query deadline
    on a running Workflow without a worker) propagates; callers answer 404/503 or fall back."""
    timeout = timedelta(seconds=QUERY_TIMEOUT_SECONDS)
    status = (await handle.describe()).status
    if status in (None, WorkflowExecutionStatus.RUNNING):
        # D-19/N34: bounded, so the SSE falls back to stored events instead of hanging.
        return {**await handle.query("status", rpc_timeout=timeout), "execution_status": "RUNNING"}
    try:
        state = await handle.query("status", rpc_timeout=timeout)
    except Exception:  # noqa: BLE001 - no worker or replay failure: use the recorded outcome
        state = await _recorded_outcome(handle, status, last_error, timeout)
    return closed_state(state, status)


async def _recorded_outcome(
    handle: WorkflowHandle,
    status: WorkflowExecutionStatus,
    last_error: LastError | None,
    timeout: timedelta,
) -> dict:
    """D-19: the closed run's outcome without a worker: Workflow result and stored `error`."""
    error = await asyncio.to_thread(last_error, handle.id) if last_error else None
    state: dict = {"phase": "failed"}
    if status == WorkflowExecutionStatus.COMPLETED:
        try:
            outcome = await handle.result(rpc_timeout=timeout)
        except Exception:  # noqa: BLE001 - unreadable result: closed, outcome unknown
            outcome = None
        state["phase"] = outcome if isinstance(outcome, str) else "unknown"
        if state["phase"] not in FAILURE_OUTCOMES:
            return state  # scaffold_complete, cancelled, ... (or unknown: never running)
        state["failure_code"] = state["phase"].upper()
        state["failure_phase"] = OUTCOME_PHASE.get(state["phase"])
    if error is not None:  # the Workflow's own report is more precise than any default
        state["failure_code"] = error["data"].get("code") or state.get("failure_code")
        state["failure_phase"] = error["data"].get("phase") or error.get("phase")
    return state


def closed_state(state: dict | None, status: WorkflowExecutionStatus) -> dict:
    base = {"phase": "created", "approval_pending": False, "cancel_requested": False}
    merged = {**base, **(state or {}), "execution_status": status.name}
    phase = merged["phase"]
    if status == WorkflowExecutionStatus.CANCELED:
        merged.update(phase="cancelled", approval_pending=False)
    elif status in _FAILED:
        if phase in WEB_PHASE:  # stopped outside Workflow code at an in-progress phase
            merged.update(
                phase="failed",
                failure_phase=merged.get("failure_phase") or (WEB_PHASE[phase] if state else None),
            )
        merged.update(
            approval_pending=False, failure_code=merged.get("failure_code") or _FAILED[status]
        )
    return merged


def stored_last_error(sessions) -> LastError:
    """`LastError` over the business DB event store (`migration_events`)."""
    from cmp_api.migration_events import MigrationEventRow

    def read(migration_id: str) -> dict | None:
        with sessions() as session:
            row = session.scalars(
                select(MigrationEventRow)
                .where(
                    MigrationEventRow.migration_id == migration_id,
                    MigrationEventRow.type == "error",
                )
                .order_by(MigrationEventRow.seq.desc())
                .limit(1)
            ).first()
            return None if row is None else {"phase": row.phase, "data": dict(row.data or {})}

    return read
