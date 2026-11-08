"""수동 단계 모드(`manual_steps`): 단계가 통과해도 사람이 "다음 단계"를 누를 때까지 기다린다."""

from __future__ import annotations

import asyncio
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


async def test_advance_during_the_observation_window_skips_the_rest_of_it() -> None:
    """기다리기 싫은 사람의 버튼: 관찰 창이 끝나기 전에 누르면 남은 시간을 건너뛰고 다음 관찰·판정으로 간다."""
    relay, store = Relay(hold=True), Store()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        _worker(env, store),
        relay.worker(env.client),
    ):
        policy = {**POLICY, "stabilize_seconds": 0, "idle_probe": False}
        handle = await _start(env, relay, store, policy)
        await until(handle, lambda s: s["actions"].get("advance"))  # 관찰 중에도 버튼이 열린다
        assert relay.calls.count(("observe", None)) == 1
        await handle.signal(TrafficShiftWorkflow.request_advance)
        # 첫 창은 아직 끝나지 않았는데(release 전) 워크플로는 창을 끝내고 다음 창을 시작했다.
        for _ in range(100):
            if relay.calls.count(("observe", None)) >= 2:
                break
            await asyncio.sleep(0.05)
        assert relay.calls.count(("observe", None)) >= 2
        relay.release.set()
        assert (await handle.result())["status"] == "DONE"
