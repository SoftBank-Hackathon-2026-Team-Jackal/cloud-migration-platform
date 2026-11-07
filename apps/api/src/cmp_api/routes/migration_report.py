"""`GET /migrations/{id}/report` (ticket 62): stage timings and a result summary from stored events.

Ticket 77 adds `cutover_outcome`: the latest `cutover.outcome` data (DB switch result) for the web
completion screen, whitelisted to the web `CutoverOutcomeData` keys.

The report is computed from `migration_events` (ticket 24) only. Stages follow the web rail
(`cloud-migration-web` `src/lib/stages.ts`): one backend phase per stage. A stage is filled once it
has ended — a later stage has events, or the Migration reached done/failed/cancelled. Only codes,
counts and identifiers are copied from event data; free-text fields (`message`, `detail`, `why`,
...) are never read, so a value that slipped past masking cannot reach the report.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from cmp_contracts.events import TERMINAL_STATUSES, MigrationEvent, MigrationStatus
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cmp_api.event_stream import StatusReader, StatusUnavailable, migration_status
from cmp_api.migration_events import list_events
from cmp_api.routes.migrations import get_event_sessions, get_status_reader

router = APIRouter(prefix="/migrations", tags=["migration scaffold"])

# Web rail stage id -> backend event phase, in rail order.
STAGES: tuple[tuple[str, str], ...] = (
    ("analysis", "analysis"),
    ("rehearsal", "build"),
    ("planning", "planning"),
    ("approval", "awaiting_approval"),
    ("provisioning", "provisioning"),
    ("data", "data_copy"),
    ("traffic", "traffic"),
    ("cutover", "verification"),
)
PAGE = 1000


class ReportStage(BaseModel):
    id: str
    phase: str
    status: str  # done | failed
    started_at: str
    ended_at: str
    seconds: int


class MigrationReport(BaseModel):
    migration_id: str
    status: MigrationStatus
    current_stage: str | None
    stages: list[ReportStage]
    total_seconds: int
    ai_fixes: int
    results: dict[str, Any]
    # Ticket 77: the latest `cutover.outcome` data in the web `CutoverOutcomeData` shape, or null.
    cutover_outcome: dict[str, Any] | None = None


SessionsDep = Annotated[Callable, Depends(get_event_sessions)]
StatusDep = Annotated[StatusReader, Depends(get_status_reader)]


def _all_events(sessions, migration_id: str) -> list[MigrationEvent]:
    events: list[MigrationEvent] = []
    with sessions() as session:
        while True:
            page = list_events(
                session, migration_id, after=events[-1].seq if events else 0, limit=PAGE
            )
            events.extend(page)
            if len(page) < PAGE:
                return events


def _time(at: str) -> datetime:
    return datetime.fromisoformat(at)


def _seconds(start: str, end: str) -> int:
    return max(0, round((_time(end) - _time(start)).total_seconds()))


def _stages(events: list[MigrationEvent], status: str) -> tuple[list[ReportStage], str | None]:
    by_phase: dict[str, list[MigrationEvent]] = {}
    for event in events:
        by_phase.setdefault(event.phase, []).append(event)
    present = [(sid, phase) for sid, phase in STAGES if phase in by_phase]
    ended = status in TERMINAL_STATUSES
    current = None if ended or not present else present[-1][0]
    stages = []
    for sid, phase in present:
        if sid == current:
            break
        own = by_phase[phase]
        failed = any(e.status == "failed" or e.type == "error" for e in own)
        stages.append(
            ReportStage(
                id=sid,
                phase=phase,
                status="failed" if failed else "done",
                started_at=own[0].at,
                ended_at=own[-1].at,
                seconds=_seconds(own[0].at, own[-1].at),
            )
        )
    return stages, current


def _last(events: list[MigrationEvent], event_type: str) -> MigrationEvent | None:
    return next((e for e in reversed(events) if e.type == event_type), None)


def _latest_by_id(events: list[MigrationEvent], event_type: str, key: str) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for event in events:
        if event.type == event_type and key in event.data:
            latest[str(event.data[key])] = event.data
    return latest


def _ai_fixes(events: list[MigrationEvent]) -> int:
    ids = set(_latest_by_id(events, "build.fix", "id"))
    for event in events:
        if event.type == "sandbox.attempt":
            ids.update(str(fix) for fix in event.data.get("fix_ids") or [])
    return len(ids)


def _results(events: list[MigrationEvent]) -> dict[str, Any]:
    sandbox_done = _last(events, "sandbox.done")
    attempts = [e.data.get("attempt") for e in events if e.type == "sandbox.attempt"]
    attempts = [a for a in attempts if isinstance(a, int)]
    build = _last(events, "build.result")
    decision = _last(events, "approval.decision")
    resources = _latest_by_id(events, "provision.resource", "id").values()
    error = _last(events, "error")
    return {
        "findings": len(_latest_by_id(events, "analysis.finding", "id")),
        "sandbox": (
            {
                "status": sandbox_done.data.get("status") if sandbox_done else None,
                "attempts": max(attempts) if attempts else None,
            }
            if sandbox_done or attempts
            else None
        ),
        "build": (
            {"status": build.data.get("status"), "reused": build.data.get("reused")}
            if build
            else None
        ),
        "approval": decision.data.get("decision") if decision else None,
        "resources": {
            "done": sum(1 for r in resources if r.get("state") == "done"),
            "failed": sum(1 for r in resources if r.get("state") == "failed"),
        },
        "data_verify": [
            {k: data.get(k) for k in ("name", "state", "source", "target")}
            for data in _latest_by_id(events, "data.verify", "name").values()
        ],
        "cutover": [
            {k: data.get(k) for k in ("id", "value", "ok")}
            for data in _latest_by_id(events, "cutover.result", "id").values()
        ],
        "error": (
            {"code": error.data.get("code"), "phase": error.data.get("phase") or error.phase}
            if error
            else None
        ),
    }


# Ticket 77: keys copied from `cutover.outcome` (web `CutoverOutcomeData`). `message` is free text
# (the executor's detail) and is never copied; `reason` is the code the web turns into a sentence.
OUTCOME_KEYS = ("result", "reason", "active", "pause_ms", "held_requests", "failed_requests")
OUTCOME_STEP_KEYS = ("id", "ms", "start_ms", "window")
OUTCOME_REPLICATION_KEYS = ("direction", "lag_ms")


def _pick(data: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: data[k] for k in keys if k in data} if isinstance(data, dict) else {}


def _cutover_outcome(events: list[MigrationEvent]) -> dict[str, Any] | None:
    event = _last(events, "cutover.outcome")
    if event is None:
        return None
    outcome = _pick(event.data, OUTCOME_KEYS)
    if isinstance(event.data.get("steps"), list):
        outcome["steps"] = [_pick(step, OUTCOME_STEP_KEYS) for step in event.data["steps"]]
    outcome["replication"] = _pick(event.data.get("replication"), OUTCOME_REPLICATION_KEYS)
    return outcome


def build_report(
    migration_id: str, events: list[MigrationEvent], state: dict | None
) -> MigrationReport:
    status = migration_status(state, events[-1] if events else None)
    stages, current = _stages(events, status)
    return MigrationReport(
        migration_id=migration_id,
        status=status,
        current_stage=current,
        stages=stages,
        total_seconds=_seconds(stages[0].started_at, stages[-1].ended_at) if stages else 0,
        ai_fixes=_ai_fixes(events),
        results=_results(events),
        cutover_outcome=_cutover_outcome(events),
    )


@router.get(
    "/{workflow_id}/report",
    response_model=MigrationReport,
    responses={404: {"description": "`MIGRATION_NOT_FOUND`: no Workflow and no stored events"}},
)
async def migration_report(
    workflow_id: str, sessions: SessionsDep, read_status: StatusDep
) -> MigrationReport:
    """Ticket 62: per-stage start/end/seconds and a result summary for the completion screen."""
    try:
        state = await read_status(workflow_id)
        exists = state is not None
    except StatusUnavailable:
        state, exists = None, True
    events = await asyncio.to_thread(_all_events, sessions, workflow_id)
    if not exists and not events:
        raise HTTPException(status_code=404, detail={"code": "MIGRATION_NOT_FOUND"})
    return build_report(workflow_id, events, state)
