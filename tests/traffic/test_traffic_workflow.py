"""TrafficShiftWorkflow(#63) 시험. Activity는 가짜로 바꿔 Temporal 위에서 흐름만 검증한다."""

from __future__ import annotations

import uuid

import pytest
from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
from cmp_worker.workflows.traffic import TrafficShiftWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

READY = {"status": "READY", "ready": True, "external_endpoint": "203.0.113.10"}
GOOD = {"requests": 100, "failed": 0, "p99_ms": 100.0}
POLICY = {"observe_seconds": 1, "min_requests": 30}


def fake_worker(client, windows, *, fail_apply_at=None, calls=None):
    calls = calls if calls is not None else []
    seq = iter(windows)

    @activity.defn(name="traffic_prepare")
    async def prepare(payload: dict) -> dict:
        calls.append(("prepare", None))
        return {"baseline": GOOD}

    @activity.defn(name="traffic_set_weight")
    async def set_weight(payload: dict) -> dict:
        calls.append(("set", payload["weights"]["target"]))
        if payload["weights"]["target"] == fail_apply_at:
            from temporalio.exceptions import ApplicationError

            raise ApplicationError("not confirmed", non_retryable=True)
        return payload

    @activity.defn(name="traffic_observe")
    async def observe(payload: dict) -> dict:
        calls.append(("observe", None))
        return next(seq)

    @activity.defn(name="traffic_rollback")
    async def rollback(payload: dict) -> dict:
        calls.append(("rollback", None))
        return {}

    return Worker(
        client,
        task_queue=MIGRATION_QUEUE,
        activities=[prepare, set_weight, observe, rollback],
    )


async def run(windows, request=None, **kw):
    calls: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        workflow_worker = Worker(
            env.client, task_queue=WORKFLOW_QUEUE, workflows=[TrafficShiftWorkflow]
        )
        async with workflow_worker, fake_worker(env.client, windows, calls=calls, **kw):
            body = {"handover": READY, "policy": POLICY, **(request or {})}
            result = await env.client.execute_workflow(
                TrafficShiftWorkflow.run,
                body,
                id=f"traffic-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )
    return result, calls


@pytest.mark.asyncio
async def test_healthy_run_goes_through_all_steps_and_finishes() -> None:
    result, calls = await run([GOOD, GOOD, GOOD])
    assert result["status"] == "DONE"
    assert result["final_weights"] == {"origin": 0, "target": 100}
    assert [c[1] for c in calls if c[0] == "set"] == [10, 50, 100]


@pytest.mark.asyncio
async def test_failed_requests_trigger_rollback_activity() -> None:
    bad = {"requests": 100, "failed": 9, "p99_ms": 100.0}
    result, calls = await run([GOOD, bad])
    assert result["status"] == "ROLLED_BACK"
    assert result["final_weights"] == {"origin": 100, "target": 0}
    assert calls[-1][0] == "rollback"


@pytest.mark.asyncio
async def test_unconfirmed_apply_rolls_back_without_observing_that_step() -> None:
    result, calls = await run([GOOD], fail_apply_at=50)
    assert result["status"] == "ROLLED_BACK"
    assert [c[0] for c in calls].count("observe") == 1


@pytest.mark.asyncio
async def test_not_ready_handover_is_blocked_before_any_activity() -> None:
    result, calls = await run([], {"handover": {"status": "UNKNOWN", "ready": True}})
    assert result["status"] == "BLOCKED"
    assert calls == []


@pytest.mark.asyncio
async def test_db_gate_required_blocks_the_last_step() -> None:
    result, calls = await run([GOOD, GOOD], {"policy": {**POLICY, "require_db_gate": True}})
    assert result["status"] == "BLOCKED"
    assert [c[1] for c in calls if c[0] == "set"] == [10, 50]


def test_workers_register_the_traffic_activities_and_workflow() -> None:
    assert create_activity_worker is not None and create_workflow_worker is not None


@pytest.mark.asyncio
async def test_workflow_history_replays_without_nondeterminism() -> None:
    from temporalio.worker import Replayer

    async with await WorkflowEnvironment.start_time_skipping() as env:
        workflow_worker = Worker(
            env.client, task_queue=WORKFLOW_QUEUE, workflows=[TrafficShiftWorkflow]
        )
        bad = {"requests": 100, "failed": 9, "p99_ms": 100.0}
        async with workflow_worker, fake_worker(env.client, [GOOD, bad]):
            handle = await env.client.start_workflow(
                TrafficShiftWorkflow.run,
                {"handover": READY, "policy": POLICY},
                id=f"traffic-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )
            await handle.result()
        history = await handle.fetch_history()
    await Replayer(workflows=[TrafficShiftWorkflow]).replay_workflow(history)
