"""One-line event/log emission for Activities (ticket 24; guide: docs/engineering/events-guide.md).

    from cmp_worker.events import emit_event
    emit_event(migration_id, "provision.check", "provisioning", "running",
               {"name": "pods_ready", "state": "done"})

Call only from Activity code (or code an Activity runs, including `asyncio.to_thread`). Workflow
code must not emit (determinism); run the `emit_migration_event` Activity instead. Emitting never
raises for bad input or storage failure: it returns None and leaves an operational log (E9).
"""

import asyncio
from dataclasses import replace
from typing import Any

from cmp_api.migration_events import Actor, EventEmitter, NullEmitter
from cmp_contracts.events import MigrationEvent
from temporalio import activity, workflow

_emitter: EventEmitter = NullEmitter()


def configure_emitter(emitter: EventEmitter) -> EventEmitter:
    """Install the process emitter (Worker startup: `DbEventEmitter(SessionLocal)`; tests:
    `InMemoryEmitter()`). Returns the previous one so tests can restore it."""
    global _emitter
    previous, _emitter = _emitter, emitter
    return previous


def current_emitter() -> EventEmitter:
    return _emitter


def _actor(across_retries: bool = False) -> Actor:
    if workflow.in_workflow():
        raise RuntimeError("emit_event is not allowed in Workflow code; use an Activity")
    if activity.in_activity():
        info = activity.info()
        actor = Actor(
            source="activity",
            workflow_id=info.workflow_id or "-",
            activity_id=info.activity_id,
            attempt=info.attempt,
            activity=info.activity_type,
        )
        # The attempt is part of the idempotency hash; leaving it out keeps one event per key
        # when Temporal runs the same Activity again (sandbox loop, ticket Provisioning 59).
        return replace(actor, attempt=0) if across_retries else actor
    return Actor(source="worker")


def emit_event(
    migration_id: str,
    type: str,
    phase: str,
    status: str,
    data: dict[str, Any],
    *,
    progress: float | None = None,
    idempotency_key: str | None = None,
    across_retries: bool = False,
) -> MigrationEvent | None:
    """Store one event. `type`/`phase`/`status`/`data` use web names; backend names listed in
    `cmp_contracts.events.to_web` are translated. `idempotency_key` distinguishes repeated
    events of the same type within one Activity attempt (default: the data Digest).
    `across_retries=True` keeps one event per key across Temporal retries of the Activity."""
    return _emitter.emit(
        migration_id,
        type,
        phase,
        status,
        data,
        progress=progress,
        idempotency_key=idempotency_key,
        actor=_actor(across_retries),
    )


async def emit_event_async(
    migration_id: str,
    type: str,
    phase: str,
    status: str,
    data: dict[str, Any],
    *,
    progress: float | None = None,
    idempotency_key: str | None = None,
    across_retries: bool = False,
) -> MigrationEvent | None:
    """`emit_event` for an async Activity body: the DB write runs in a thread."""
    actor = _actor(across_retries)
    return await asyncio.to_thread(
        _emitter.emit,
        migration_id,
        type,
        phase,
        status,
        data,
        progress=progress,
        idempotency_key=idempotency_key,
        actor=actor,
    )


def log_event(
    migration_id: str, level: str, code: str, fields: dict[str, Any] | None = None
) -> dict | None:
    """Operational log for the agent: level debug/info/warning/error, an UPPER_SNAKE code and
    small masked fields. No user text, no secret values."""
    return _emitter.log(migration_id, level, code, fields, actor=_actor())


@activity.defn(name="emit_migration_event")
async def emit_migration_event(request: dict) -> int | None:
    """For Workflow code that must emit: `{migration_id, type, phase, status, data, progress?,
    idempotency_key?, log?: {level, code, fields}}` → stored `seq` (None when rejected).
    Registered on the Provisioning queue. `log` also writes an operational log (ticket 49)."""
    entry = request.get("log")
    if entry is not None:
        actor = _actor()
        await asyncio.to_thread(
            _emitter.log,
            request["migration_id"],
            entry["level"],
            entry["code"],
            entry.get("fields"),
            actor=actor,
        )
    event = await emit_event_async(
        request["migration_id"],
        request["type"],
        request["phase"],
        request["status"],
        request.get("data", {}),
        progress=request.get("progress"),
        idempotency_key=request.get("idempotency_key"),
    )
    return None if event is None else event.seq
