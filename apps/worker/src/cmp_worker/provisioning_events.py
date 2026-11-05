"""Provisioning-owned events (ticket 24): `plan.ready`, `provision.resource`, `provision.check`.

Workflow heartbeat strings (`started`, `running`, `connect`, `helm:<name>`, ...) stay command-line
values for Temporal liveness; these helpers emit the web events at step boundaries instead (E16).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from cmp_contracts.plan import Preview
from cmp_providers.terraform_events import ApplyProgress

from cmp_worker.events import emit_event, log_event

PHASE = "provisioning"
# AWS/GCP workloads (09/11): DB release, DB pods, DB connect, App release, App pods, health, LB.
WORKLOAD_STEPS = 7


def changed_resources(preview: Preview) -> int:
    """`m` for Terraform progress when Apply prints no `planned_change` lines."""
    return sum(
        change.quantity or 1
        for change in preview.changes
        if change.action in ("CREATE", "UPDATE", "DELETE")
    )


def terraform_progress(migration_id: str, preview: Preview) -> ApplyProgress:
    """`provision.resource` per Terraform resource start/complete/error; progress = n/m."""

    def on_resource(data: dict, progress: float) -> None:
        emit_event(
            migration_id,
            "provision.resource",
            PHASE,
            "failed" if data["state"] == "failed" else "running",
            data,
            progress=progress,
            idempotency_key=f"tf:{data['id']}:{data['state']}",
        )

    return ApplyProgress(on_resource, expected_total=changed_resources(preview))


# ---- workloads (k8s_workloads base for AWS/GCP 09/11) ----


@dataclass
class WorkloadRun:
    migration_id: str
    done: int = 0

    def progress(self) -> float:
        return round(100 * min(self.done, WORKLOAD_STEPS) / WORKLOAD_STEPS, 1)


_RUN: ContextVar[WorkloadRun | None] = ContextVar("cmp_workload_run", default=None)


def start_workload_run(migration_id: str) -> None:
    """Bind events of this Activity invocation to its Migration (per asyncio task context)."""
    _RUN.set(WorkloadRun(migration_id))


def _emit(event_type: str, data: dict, key: str) -> None:
    run = _RUN.get()
    if run is None:
        return  # not inside a workload execution (unit tests of helpers)
    if data["state"] == "done":
        run.done += 1
    if event_type == "provision.resource":
        data = {**data, "progress": run.progress()}
    emit_event(
        run.migration_id,
        event_type,
        PHASE,
        "failed" if data["state"] == "failed" else "running",
        data,
        progress=run.progress(),
        idempotency_key=key,
    )


def workload_resource(resource_id: str, key: str, state: str, name: str) -> None:
    _emit(
        "provision.resource",
        {"id": resource_id, "key": key, "state": state, "progress": 0, "name": name},
        f"res:{resource_id}:{state}",
    )


def workload_check(name: str, state: str, target: str) -> None:
    """`pods_ready` · `db_connect` · `app_health`; `target` (release name) keeps keys distinct."""
    _emit("provision.check", {"name": name, "state": state}, f"chk:{name}:{target}:{state}")


@contextmanager
def resource_step(resource_id: str, key: str, name: str) -> Iterator[None]:
    """`provision.resource` running → done, or failed when the step raises."""
    workload_resource(resource_id, key, "running", name)
    try:
        yield
    except Exception:
        workload_resource(resource_id, key, "failed", name)
        raise
    workload_resource(resource_id, key, "done", name)


@contextmanager
def checked(name: str, target: str) -> Iterator[None]:
    """`provision.check` running → done, or failed when the check raises."""
    workload_check(name, "running", target)
    try:
        yield
    except Exception:
        workload_check(name, "failed", target)
        raise
    workload_check(name, "done", target)


def log_result(migration_id: str, step: str, result: dict) -> dict:
    """Operational log of a Provisioning step outcome (codes and refs names only)."""
    level = "info" if result.get("status") == "SUCCEEDED" else "error"
    log_event(
        migration_id,
        level,
        str(result.get("code") or "UNKNOWN"),
        {
            "step": step,
            "status": result.get("status"),
            "refs": sorted(result.get("resource_refs") or {}),
        },
    )
    return result


def log_workload_result(step: str, result: dict) -> None:
    run = _RUN.get()
    if run is not None:
        log_result(run.migration_id, step, result)


# ---- plan.ready ----


def emit_plan_ready(migration_id: str, result: dict) -> None:
    if result.get("status") != "PUBLISHED":
        return
    emit_event(
        migration_id,
        "plan.ready",
        "planning",
        "done",
        {
            "plan_id": result["plan_id"],
            "revision": result["revision"],
            "manifest_digest": result["manifest_digest"],
            "preview_digest": result["preview_digest"],
        },
        idempotency_key=f"{result['plan_id']}:{result['revision']}",
    )


class PlanReadyEvents:
    """Wraps a `PlanPublisher`: each `publish*(migration_id, ...)` emits `plan.ready` on success.

    A wrapper keeps the publisher (and the Activity class) unchanged while ticket 22 edits them.
    """

    def __init__(self, publisher):
        self._publisher = publisher

    def __getattr__(self, name: str):
        attribute = getattr(self._publisher, name)
        if not name.startswith("publish") or not callable(attribute):
            return attribute

        def publish(migration_id: str, *args, **kwargs):
            result = attribute(migration_id, *args, **kwargs)
            emit_plan_ready(migration_id, result)
            return result

        return publish
