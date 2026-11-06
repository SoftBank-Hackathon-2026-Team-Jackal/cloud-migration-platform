"""DataMigrationWorkflow(ticket 05) 시험. Activity는 가짜로 바꿔 Temporal 위에서 흐름만 검증한다."""

from __future__ import annotations

import asyncio
import uuid

from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from cmp_worker.workflows.data_migration import DataMigrationWorkflow
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

ARGS = {
    "request": {"migration_id": "migration-x"},
    "app": {},
    "settings": {},
    "sample_interval_s": 1,
    "monitor_interval_s": 1,
}


class Fakes:
    def __init__(
        self,
        *,
        precheck_ok=True,
        phases=("copy", "catchup", "streaming"),
        refuse=False,
        fail: str | None = None,
        cleanup_fails=False,
    ):
        self.calls: list[str] = []
        self.phases = list(phases)
        self.precheck_ok, self.refuse = precheck_ok, refuse
        self.fail, self.cleanup_fails = fail, cleanup_fails

    def activities(self):
        def simple(name, result):
            @activity.defn(name=name)
            async def fn(payload: dict) -> dict:
                self.calls.append(name)
                if name == self.fail:
                    raise ApplicationError(name, type="DB_COMMAND_FAILED", non_retryable=True)
                if name == "dm_cleanup" and self.cleanup_fails:
                    raise ApplicationError(name, type="DbAccessNotConfigured", non_retryable=True)
                return result

            return fn

        @activity.defn(name="dm_precheck")
        async def precheck(payload: dict) -> dict:
            self.calls.append("dm_precheck")
            return {"ok": self.precheck_ok, "actions": [], "blockers": [], "warnings": []}

        @activity.defn(name="dm_sample")
        async def sample(payload: dict) -> dict:
            self.calls.append(f"dm_sample:{payload['direction']}")
            phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
            return {"phase": phase, "lag_bytes": 0, "tables": 1, "tables_ready": 1}

        @activity.defn(name="dm_switchover")
        async def switchover(payload: dict) -> dict:
            direction = payload["request"]["direction"]
            self.calls.append(f"dm_switchover:{direction}:{payload['attempt']}")
            if self.refuse:
                raise ApplicationError("NOT_READY", type="NOT_READY", non_retryable=True)
            active = "target" if direction == "forward" else "source"
            return {"switched": True, "active": active, "write_pause_ms": 7, "verify_ok": True}

        @activity.defn(name="dm_announce_cutover")
        async def announce(payload: dict) -> dict:
            self.calls.append(f"dm_announce_cutover:{payload['approval']}")
            return {"ready": True}

        return [
            announce,
            precheck,
            sample,
            switchover,
            simple("dm_prepare", {}),
            simple("dm_copy_schema", {"copied": True}),
            simple("dm_subscribe", {}),
            simple("dm_readiness", {"ready": True}),
            simple("dm_cleanup", {}),
            simple("dm_install_cutover", {}),
            simple("dm_route_app", {}),
        ]


async def start(env, fakes):
    workers = [
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[DataMigrationWorkflow]),
        Worker(env.client, task_queue=MIGRATION_QUEUE, activities=fakes.activities()),
    ]
    handle = await env.client.start_workflow(
        DataMigrationWorkflow.run, ARGS, id=f"data-{uuid.uuid4()}", task_queue=WORKFLOW_QUEUE
    )
    return workers, handle


async def wait_phase(handle, phase, tries=200):
    for _ in range(tries):
        if (await handle.query(DataMigrationWorkflow.status))["phase"] == phase:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"phase {phase} not reached")


async def wait_announced(fakes, tries=200):
    """phase가 streaming이 된 뒤 첫 감시 주기에 전환 계획을 알린다. 그 전에 전환 신호를 보내면
    알림보다 전환이 먼저 처리돼 호출 순서가 달라지므로, 첫 알림을 기다린 뒤 신호를 보낸다."""
    for _ in range(tries):
        if "dm_announce_cutover:True" in fakes.calls:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("cutover not announced")


async def test_replicates_until_streaming_then_switches_rolls_back_and_finishes() -> None:
    fakes = Fakes()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        workers, handle = await start(env, fakes)
        async with workers[0], workers[1]:
            await wait_phase(handle, "streaming")
            assert (await handle.query(DataMigrationWorkflow.db_gate))["writer"] == "source"
            await wait_announced(fakes)
            await handle.signal(DataMigrationWorkflow.switchover)
            await wait_phase(handle, "switched")
            gate = await handle.query(DataMigrationWorkflow.db_gate)
            assert gate == {"writer": "target", "reverse_replication_ok": True}
            await handle.signal(DataMigrationWorkflow.rollback)
            await wait_phase(handle, "streaming")
            await handle.signal(DataMigrationWorkflow.finish)
            result = await handle.result()
    assert result["status"] == "done" and result["active"] == "source"
    assert [s["active"] for s in result["switches"]] == ["target", "source"]
    order = [c for c in fakes.calls if not c.startswith(("dm_sample", "dm_readiness"))]
    assert order[:6] == ["dm_precheck", "dm_prepare", "dm_copy_schema", "dm_subscribe",
                         "dm_install_cutover", "dm_route_app"]  # fmt: skip
    assert "dm_sample:reverse" in fakes.calls  # 전환 뒤에는 역방향 복제를 감시한다
    # 원본이 쓰기 주체일 때만 계획과 승인 요청을 알린다: 처음 한 번, 롤백 뒤(새 시도) 한 번
    announced = [c for c in order if c.startswith(("dm_announce", "dm_switchover"))]
    assert announced == ["dm_announce_cutover:True", "dm_switchover:forward:1",
                         "dm_switchover:reverse:2", "dm_announce_cutover:True"]  # fmt: skip


async def test_blocked_precheck_creates_nothing() -> None:
    fakes = Fakes(precheck_ok=False)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        workers, handle = await start(env, fakes)
        async with workers[0], workers[1]:
            result = await handle.result()
    assert result["status"] == "blocked" and result["failure_code"] == "PRECHECK_BLOCKED"
    assert fakes.calls == ["dm_precheck"]


async def test_refused_switchover_keeps_the_source_as_writer() -> None:
    fakes = Fakes(refuse=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        workers, handle = await start(env, fakes)
        async with workers[0], workers[1]:
            await wait_phase(handle, "streaming")
            await wait_announced(fakes)
            await handle.signal(DataMigrationWorkflow.switchover)
            for _ in range(200):
                status = await handle.query(DataMigrationWorkflow.status)
                if status["switches"]:
                    break
                await asyncio.sleep(0.05)
            assert status["switches"][0] == {
                "switched": False,
                "code": "NOT_READY",
                "from": "source",
            }
            assert status["active"] == "source" and status["phase"] == "streaming"
            await handle.signal(DataMigrationWorkflow.request_cancel)
            result = await handle.result()
    assert result["status"] == "cancelled"
    assert "dm_cleanup" in fakes.calls
    # 거부 뒤에는 새 시도이므로 승인 요청을 다시 보낸다
    assert fakes.calls.count("dm_announce_cutover:True") == 2


async def run_to_end(fakes, args=None):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        workers = [
            Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[DataMigrationWorkflow]),
            Worker(env.client, task_queue=MIGRATION_QUEUE, activities=fakes.activities()),
        ]
        async with workers[0], workers[1]:
            return await env.client.execute_workflow(
                DataMigrationWorkflow.run,
                args or ARGS,
                id=f"data-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )


async def test_failed_replication_cleans_up_the_slot_and_keeps_the_failure_code() -> None:
    """Provisioning 72 (#298): 구독을 만든 뒤 실패하면 원본 슬롯이 WAL을 붙잡지 않게 정리한다."""
    fakes = Fakes(fail="dm_install_cutover")
    result = await run_to_end(fakes)
    assert result["status"] == "failed" and result["failure_code"] == "DB_COMMAND_FAILED"
    assert fakes.calls[-1] == "dm_cleanup"
    assert result["cleanup_code"] is None


async def test_failed_cleanup_is_reported_without_hiding_the_original_failure() -> None:
    fakes = Fakes(fail="dm_subscribe", cleanup_fails=True)
    result = await run_to_end(fakes)
    assert result["failure_code"] == "DB_COMMAND_FAILED"
    assert result["cleanup_code"] == "DbAccessNotConfigured"
    assert fakes.calls.count("dm_cleanup") == 1  # 재시도하지 않는 오류는 한 번만


async def test_sync_timeout_cleans_up_too() -> None:
    fakes = Fakes(phases=("copy",))
    result = await run_to_end(fakes, ARGS | {"sync_timeout_s": 3})
    assert result["failure_code"] == "SYNC_TIMEOUT" and "dm_cleanup" in fakes.calls


async def test_failure_before_subscribe_creates_nothing_to_clean() -> None:
    fakes = Fakes(fail="dm_copy_schema")
    result = await run_to_end(fakes)
    assert result["failure_code"] == "DB_COMMAND_FAILED"
    assert "dm_cleanup" not in fakes.calls and result["cleanup_code"] is None
