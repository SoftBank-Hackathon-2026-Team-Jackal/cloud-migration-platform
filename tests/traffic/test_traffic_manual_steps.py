"""수동 단계 모드(`manual_steps`): 단계가 통과해도 사람이 "다음 단계"를 누를 때까지 기다린다."""

from __future__ import annotations

import uuid

from cmp_contracts.queues import WORKFLOW_QUEUE
from cmp_worker.workflows.traffic import TrafficShiftWorkflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_traffic_actions import POLICY, Relay, Store, body, until

MANUAL = {**POLICY, "manual_steps": True, "stabilize_seconds": 0}


async def _start(env, relay, store, policy):
    return await env.client.start_workflow(
        TrafficShiftWorkflow.run,
        body(policy=policy),
        id=f"traffic-{uuid.uuid4()}",
        task_queue=WORKFLOW_QUEUE,
    )


def _worker(env, store):
    return Worker(
        env.client,
        task_queue=WORKFLOW_QUEUE,
        workflows=[TrafficShiftWorkflow],
        activities=store.activities(),
    )


async def test_waits_at_each_gate_until_advance() -> None:
    relay, store = Relay(), Store()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        _worker(env, store),
        relay.worker(env.client),
    ):
        handle = await _start(env, relay, store, {**MANUAL, "manual_wait_seconds": 14000})
        status = await until(handle, lambda s: s["actions"].get("advance"))
        assert status["snapshot"]["step"] == 10
        assert status["snapshot"]["notes"][0]["key"] == "awaitNext"
        assert not [c for c in relay.calls if c == ("set", 50)]  # 아직 50%로 가지 않았다
        await handle.signal(TrafficShiftWorkflow.request_advance)
        status = await until(
            handle,
            lambda s: s["actions"].get("advance") and s["snapshot"]["step"] == 50,
        )
        assert status["snapshot"]["notes"][0]["params"] == {"pct": 50, "next": 100}
        await handle.signal(TrafficShiftWorkflow.request_advance)
        result = await handle.result()
        assert result["status"] == "DONE"
        assert result["final_weights"]["target"] == 100


async def test_rollback_while_waiting() -> None:
    relay, store = Relay(), Store()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        _worker(env, store),
        relay.worker(env.client),
    ):
        handle = await _start(env, relay, store, {**MANUAL, "manual_wait_seconds": 14000})
        await until(handle, lambda s: s["actions"].get("advance"))
        await handle.signal(TrafficShiftWorkflow.request_rollback)
        result = await handle.result()
        assert result["status"] == "ROLLED_BACK"
        assert ("rollback", None) in relay.calls


async def test_wait_timeout_continues_and_off_by_default() -> None:
    relay, store = Relay(), Store()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        _worker(env, store),
        relay.worker(env.client),
    ):
        handle = await _start(env, relay, store, {**MANUAL, "manual_wait_seconds": 5})
        assert (await handle.result())["status"] == "DONE"
    relay2, store2 = Relay(), Store()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        _worker(env, store2),
        relay2.worker(env.client),
    ):
        handle = await _start(env, relay2, store2, {**POLICY, "stabilize_seconds": 0})
        assert (await handle.result())["status"] == "DONE"
