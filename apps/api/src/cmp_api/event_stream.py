"""SSE body for `GET /migrations/{id}/events` (ticket 24, decisions E10-E12).

Snapshot first, then stored events with `id: <seq>`, a heartbeat comment when idle, and the
legacy `event: status` block while the web moves over. The stream closes after the Migration
reaches done/failed/cancelled and the remaining events are sent.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from cmp_contracts.events import TERMINAL_STATUSES, MigrationEvent, utc_now
from cmp_contracts.migration import WEB_PHASE
from sqlalchemy.orm import Session

from cmp_api.migration_events import list_events

HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.5
STATUS_EVERY_SECONDS = 1.0
SNAPSHOT_LIMIT = 5000
PAGE = 500

StatusReader = Callable[[str], Awaitable[dict | None]]

# MigrationWorkflow phase -> web BackendPhase (E17): one table, `cmp_contracts.migration.WEB_PHASE`
# (ticket 49 N3: `planning_handoff`/`workload_planning` are in progress, not failures).
RUNNING_PHASES = set(WEB_PHASE) - {"awaiting_approval"}


class StatusUnavailable(Exception):
    """Temporal could not answer; fall back to the stored events."""


def migration_status(state: dict | None, last: MigrationEvent | None) -> str:
    """Web `MigrationStatus` from the Workflow query (E11), or from the last event without one."""
    if state is not None:
        phase = state.get("phase")
        # D-19: a closed run is never running or waiting, whatever phase it last reported.
        closed = state.get("execution_status") not in (None, "RUNNING")
        if phase == "scaffold_complete":
            return "done"
        if phase == "cancelled":
            return "cancelled"
        if closed:
            return "failed"  # closed but not done/cancelled: plan_failed, failed, unknown, ...
        if phase == "awaiting_approval" or state.get("review_pending"):
            return "waiting"
        if phase in RUNNING_PHASES:
            return "running"
        return "failed"  # failed, plan_failed, plan_rejected, approval_expired, ...
    if last is None:
        return "idle"
    if last.status == "failed" or last.type == "error":
        return "failed"
    if (
        last.type == "cutover.handover"
        and last.data.get("item") == "access"
        and last.status == "done"
    ):
        return "done"
    return "waiting" if last.status == "waiting" else "running"


def _event_status(status: str) -> str:
    return {"idle": "running", "cancelled": "failed"}.get(status, status)


def _block(payload: dict, *, event_id: int | None = None, event: str | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event:
        lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def parse_last_event_id(value: str | None) -> int | None:
    if value is None or not value.strip().isdigit():
        return None
    return int(value.strip())


def _read(sessions, migration_id: str, after: int, limit: int) -> list[MigrationEvent]:
    with sessions() as session:
        return list_events(session, migration_id, after=after, limit=limit)


def snapshot(
    migration_id: str,
    status: str,
    events: list[MigrationEvent],
    *,
    reset: bool,
    seq: int,
    state: dict | None,
) -> dict:
    if events:
        phase = events[-1].phase
    else:
        state = state or {}  # ticket 49 F9: a failure snapshot shows the failed phase
        phase = WEB_PHASE.get(state.get("phase")) or state.get("failure_phase") or "analysis"
    return {
        "seq": seq,
        "at": utc_now(),
        "phase": phase,
        "type": "snapshot",
        "status": _event_status(status),
        "data": {
            "migration_id": migration_id,
            "status": status,
            "reset": reset,
            "events": [event.sse_payload() for event in events],
        },
    }


async def stream_events(
    migration_id: str,
    *,
    sessions: Callable[[], Session],
    read_status: StatusReader,
    last_event_id: int | None,
    legacy: bool,
    initial_state: dict | None,
) -> AsyncIterator[str]:
    state = initial_state
    last = None
    resumed = bool(last_event_id)
    if resumed:  # the last event the client saw decides the status without a Workflow
        seen = await asyncio.to_thread(_read, sessions, migration_id, last_event_id - 1, 1)
        last = seen[-1] if seen and seen[-1].seq == last_event_id else None
        resumed = last is not None  # an id we never stored (store reset): start over
    events = (
        [] if resumed else await asyncio.to_thread(_read, sessions, migration_id, 0, SNAPSHOT_LIMIT)
    )
    cursor = last_event_id if resumed else (events[-1].seq if events else 0)
    if not resumed:
        last = events[-1] if events else None
    status = migration_status(state, last)
    yield _block(
        snapshot(migration_id, status, events, reset=not resumed, seq=cursor, state=state),
        event_id=cursor,
    )
    legacy_state = None
    if legacy and state is not None:
        yield _block(state, event="status")
        legacy_state = state
    sent_at = time.monotonic()
    status_at = time.monotonic()
    while True:
        page = await asyncio.to_thread(_read, sessions, migration_id, cursor, PAGE)
        for event in page:
            yield _block(event.sse_payload(), event_id=event.seq)
            cursor, last = event.seq, event
            sent_at = time.monotonic()
        if len(page) == PAGE:
            continue  # drain backlog before sleeping
        if status in TERMINAL_STATUSES:
            return  # remaining events were just sent
        if time.monotonic() - status_at >= STATUS_EVERY_SECONDS:
            status_at = time.monotonic()
            try:
                state = await read_status(migration_id) or state
            except StatusUnavailable:
                pass
            if legacy and state is not None and state != legacy_state:
                yield _block(state, event="status")
                legacy_state = state
                sent_at = time.monotonic()
        status = migration_status(state, last)
        if status in TERMINAL_STATUSES:
            continue  # one more read so events stored before the terminal state are sent
        if time.monotonic() - sent_at >= HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            sent_at = time.monotonic()
        await asyncio.sleep(POLL_SECONDS)
