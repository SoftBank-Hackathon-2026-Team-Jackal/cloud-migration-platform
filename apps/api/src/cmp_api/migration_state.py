"""MigrationWorkflow state for the status API and SSE (ticket 49 F8).

The `status` query only knows what Workflow code recorded. A Workflow that was terminated, timed
out or failed outside its own code keeps answering its last in-progress phase, so the Temporal
execution status decides first: a closed failure is never reported as `running`.
"""

from datetime import timedelta

from cmp_contracts.migration import WEB_PHASE
from temporalio.client import WorkflowExecutionStatus, WorkflowHandle

QUERY_TIMEOUT_SECONDS = 5.0  # a closed Workflow is replayed by a worker; none may be running

_FAILED = {
    WorkflowExecutionStatus.FAILED: "WORKFLOW_FAILED",
    WorkflowExecutionStatus.TERMINATED: "WORKFLOW_TERMINATED",
    WorkflowExecutionStatus.TIMED_OUT: "WORKFLOW_TIMED_OUT",
}


async def read_migration_state(handle: WorkflowHandle) -> dict:
    """Query state with the execution status applied. `RPCError` (e.g. NOT_FOUND) propagates."""
    status = (await handle.describe()).status
    if status in (None, WorkflowExecutionStatus.RUNNING):
        return {**await handle.query("status"), "execution_status": "RUNNING"}
    try:
        state = await handle.query("status", rpc_timeout=timedelta(seconds=QUERY_TIMEOUT_SECONDS))
    except Exception:  # noqa: BLE001 - no worker or replay failure: describe is enough
        state = None
    return closed_state(state, status)


def closed_state(state: dict | None, status: WorkflowExecutionStatus) -> dict:
    base = {"phase": "created", "approval_pending": False, "cancel_requested": False}
    merged = {**base, **(state or {}), "execution_status": status.name}
    phase = merged["phase"]
    if status == WorkflowExecutionStatus.CANCELED:
        merged.update(phase="cancelled", approval_pending=False)
    elif status in _FAILED and (phase in WEB_PHASE or state is None):
        merged.update(
            phase="failed",
            approval_pending=False,
            failure_code=merged.get("failure_code") or _FAILED[status],
            failure_phase=merged.get("failure_phase")
            or (WEB_PHASE.get(phase) if state is not None else None),
        )
    return merged
