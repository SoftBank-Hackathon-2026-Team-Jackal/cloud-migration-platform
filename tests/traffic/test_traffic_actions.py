"""#234: 실제 모드 트래픽 이벤트(traffic.prepare·traffic.ready)와 사람이 누르는 롤백·재시도 Signal.

TrafficShiftWorkflow는 진짜, 중계 서버 Activity·이벤트 저장·데이터 이전 조회는 가짜다. 저장된 이벤트는
웹 계약(`validate_data`)으로 검증하고, 비밀값이 없고 같은 이벤트가 두 번 저장되지 않는지 본다.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from cmp_contracts.events import validate_data
from cmp_contracts.queues import WORKFLOW_QUEUE
from cmp_traffic import Handover, TrafficPolicy
from cmp_traffic.snapshot import build_prepare, build_ready, prepare_failure
from cmp_worker.activities import traffic as traffic_activities
from cmp_worker.workflows.traffic import TrafficShiftWorkflow
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

RELAY = "relay-test"
GOOD = {"requests": 100, "failed": 0, "p99_ms": 100.0}
BAD = {"requests": 100, "failed": 5, "p99_ms": 100.0}
HANDOVER = {
    "status": "READY",
    "ready": True,
    "external_endpoint": "http://10.200.0.3:8080",
    "health_path": "/healthz",
    "port": 8080,
}
POLICY = {"observe_seconds": 1, "min_requests": 30}
READINESS = {"lag_ms": 12.0, "longest_tx_ms": 85, "commits_per_sec": 118.0}


class Relay:
    """가짜 중계 서버. `hold`가 켜져 있으면 관찰 창이 `release`될 때까지 끝나지 않는다."""

    def __init__(
        self, windows=None, *, prepare_error: str | None = None, hold: bool = False, pace=0.0
    ):
        self.pace = pace  # 관찰 창 하나에 드는 실제 시간(안정화 구간이 너무 빨리 돌지 않게)
        self.windows = list(windows or [])
        self.prepare_error = prepare_error
        self.calls: list = []
        self.release = asyncio.Event()
        if not hold:
            self.release.set()

    def worker(self, client) -> Worker:
        @activity.defn(name="traffic_prepare")
        async def prepare(payload: dict) -> dict:
            self.calls.append(("prepare", None))
            if self.prepare_error:
                raise ApplicationError("no", type=self.prepare_error, non_retryable=True)
            checks = [
                {
                    "id": "external_endpoint",
                    "state": "done",
                    "value": HANDOVER["external_endpoint"],
                },
                {"id": "health", "state": "done", "value": "/healthz 200"},
            ]
            return {"baseline": GOOD, "checks": checks}

        @activity.defn(name="traffic_set_weight")
        async def set_weight(payload: dict) -> dict:
            self.calls.append(("set", payload["weights"]["target"]))
            return payload

        @activity.defn(name="traffic_observe")
        async def observe(payload: dict) -> dict:
            self.calls.append(("observe", None))
            await asyncio.wait_for(self.release.wait(), timeout=30)
            await asyncio.sleep(self.pace)
            return self.windows.pop(0) if self.windows else GOOD

        @activity.defn(name="traffic_rollback")
        async def rollback(payload: dict) -> dict:
            self.calls.append(("rollback", None))
            return {}

        return Worker(client, task_queue=RELAY, activities=[prepare, set_weight, observe, rollback])


class Store:
    """가짜 `emit_migration_event`·`traffic_readiness`(Workflow 큐)."""

    def __init__(self, readiness: dict | None = None):
        self.events: list[dict] = []
        self.readiness = readiness

    def activities(self) -> list:
        @activity.defn(name="emit_migration_event")
        async def emit(request: dict) -> int:
            validate_data(request["type"], request["data"])  # the web contract accepts it
            self.events.append(request)
            return len(self.events)

        @activity.defn(name="traffic_readiness")
        async def readiness(payload: dict) -> dict:
            if self.readiness is None:
                raise ApplicationError("no data migration", non_retryable=True)
            return self.readiness

        return [emit, readiness]

    def of(self, event_type: str) -> list[dict]:
        return [event for event in self.events if event["type"] == event_type]


def body(migration_id: str | None = "migration-1", **extra) -> dict:
    request = {"handover": HANDOVER, "policy": POLICY, "relay_queue": RELAY, **extra}
    if migration_id:
        request["migration_id"] = migration_id
    return request


async def run_to_end(relay: Relay, store: Store, request: dict) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[TrafficShiftWorkflow],
            activities=store.activities(),
        ),
        relay.worker(env.client),
    ):
        return await env.client.execute_workflow(
            TrafficShiftWorkflow.run,
            request,
            id=f"traffic-{uuid.uuid4()}",
            task_queue=WORKFLOW_QUEUE,
        )


async def until(handle, check, *, tries: int = 200) -> dict:
    for _ in range(tries):
        status = await handle.query(TrafficShiftWorkflow.status)
        if check(status):
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached: {status}")


# ---- 순수: 페이로드 ----


def test_prepare_payload_carries_checks_relay_target_and_policy_only() -> None:
    data = build_prepare(
        state="running",
        handover=Handover.model_validate(HANDOVER),
        policy=TrafficPolicy.model_validate(POLICY),
        checks=[{"id": "handoff_status", "state": "done", "value": "READY"}],
    )
    validate_data("traffic.prepare", data)
    assert data["target"] == {"name": "10.200.0.3", "external_endpoint": "http://10.200.0.3:8080"}
    assert data["relay"] == {"name": "Envoy", "mode": "weighted_clusters"}
    assert data["policy"]["steps"] == [10, 50, 100]


def test_an_unhealthy_target_fails_the_health_check_after_a_done_endpoint() -> None:
    checks, reason = prepare_failure("TargetUnhealthy", Handover.model_validate(HANDOVER))
    assert reason == "TARGET_UNHEALTHY"
    assert [(c["id"], c["state"]) for c in checks] == [
        ("handoff_status", "done"),
        ("ready", "done"),
        ("external_endpoint", "done"),
        ("health", "failed"),
    ]
    checks, reason = prepare_failure("SomethingElse", Handover.model_validate(HANDOVER))
    assert reason == "PREPARE_FAILED" and checks[-1] == {
        "id": "external_endpoint",
        "state": "failed",
    }


def test_ready_payload_is_omitted_when_a_value_is_unknown() -> None:
    assert build_ready("done", READINESS) == {"state": "done", **READINESS}
    assert build_ready("done", {**READINESS, "lag_ms": None}) is None
    assert build_ready("done", {}) is None


# ---- Workflow: 이벤트 ----


async def test_events_follow_the_web_order_ready_prepare_then_snapshots() -> None:
    store = Store(READINESS)
    result = await run_to_end(Relay([GOOD] * 3), store, body())
    assert result["status"] == "DONE"
    kinds = [(e["type"], e["data"].get("state") or e["data"].get("phase")) for e in store.events]
    assert kinds[:5] == [
        ("traffic.ready", "running"),
        ("traffic.prepare", "running"),
        ("traffic.prepare", "done"),
        ("traffic.ready", "done"),
        ("traffic.snapshot", "applying"),
    ]
    prepare_done = store.of("traffic.prepare")[-1]
    assert prepare_done["status"] == "done"
    assert [c["id"] for c in prepare_done["data"]["checks"]] == [
        "handoff_status",
        "ready",
        "external_endpoint",
        "health",
    ]
    assert {e["migration_id"] for e in store.events} == {"migration-1"}
    assert {e["phase"] for e in store.events} == {"traffic"}


async def test_no_event_is_stored_twice_and_no_secret_is_sent() -> None:
    store = Store(READINESS)
    await run_to_end(Relay([GOOD] * 3), store, body())
    keys = [e["idempotency_key"] for e in store.events if "idempotency_key" in e]
    assert len(keys) == len(set(keys)) == 4  # ready ×2, prepare ×2
    dump = json.dumps(store.events).lower()
    for word in ("password", "secret", "token", "credential"):
        assert word not in dump


async def test_unknown_readiness_sends_no_ready_event() -> None:
    store = Store(None)
    await run_to_end(Relay([GOOD] * 3), store, body())
    assert store.of("traffic.ready") == []
    assert store.of("traffic.prepare")


async def test_db_gate_that_blocks_the_last_step_reports_ready_waiting() -> None:
    store = Store(READINESS)
    policy = {**POLICY, "require_db_gate": True}
    result = await run_to_end(Relay([GOOD] * 3), store, body(policy=policy))
    assert result["status"] == "BLOCKED"
    assert [e["data"]["state"] for e in store.of("traffic.ready")] == ["running", "waiting"]


async def test_handover_not_ready_is_a_failed_prepare_without_relay_calls() -> None:
    store, relay = Store(READINESS), Relay()
    handover = {**HANDOVER, "status": "UNKNOWN"}
    result = await run_to_end(relay, store, body(handover=handover))
    assert result["status"] == "BLOCKED" and relay.calls == []
    (failed,) = store.of("traffic.prepare")
    assert failed["status"] == "failed" and failed["data"]["reason"] == "HANDOVER_NOT_READY"
    assert failed["data"]["checks"][0] == {
        "id": "handoff_status",
        "state": "failed",
        "value": "UNKNOWN",
    }


async def test_unhealthy_target_is_a_failed_prepare_and_blocks_the_shift() -> None:
    store, relay = Store(READINESS), Relay(prepare_error="TargetUnhealthy")
    result = await run_to_end(relay, store, body())
    assert result["status"] == "BLOCKED"
    last = store.of("traffic.prepare")[-1]
    assert last["status"] == "failed" and last["data"]["reason"] == "TARGET_UNHEALTHY"
    assert store.of("traffic.snapshot") == []


async def test_without_migration_id_nothing_is_emitted_and_rollback_ends_the_run() -> None:
    store = Store(READINESS)
    result = await run_to_end(Relay([BAD]), store, body(migration_id=None))
    assert result["status"] == "ROLLED_BACK" and store.events == []


async def test_a_gate_rollback_waits_for_a_person_then_reports_failure_when_nobody_retries() -> (
    None
):
    store, relay = Store(READINESS), Relay([BAD])
    result = await run_to_end(relay, store, body(retry_wait_seconds=60))
    assert result["status"] == "ROLLED_BACK"
    assert relay.calls.count(("rollback", None)) == 1  # no automatic retry
    rolled = [e for e in store.of("traffic.snapshot") if e["data"]["phase"] == "rolledback"]
    assert [e["status"] for e in rolled] == ["running", "failed"]


# ---- Workflow: 사람이 누르는 롤백·재시도 ----


async def test_manual_rollback_then_retry_runs_the_shift_again_to_the_end() -> None:
    store, relay = Store(READINESS), Relay(hold=True)
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[TrafficShiftWorkflow],
            activities=store.activities(),
        ),
        relay.worker(env.client),
    ):
        handle = await env.client.start_workflow(
            TrafficShiftWorkflow.run,
            body(),
            id=f"traffic-{uuid.uuid4()}",
            task_queue=WORKFLOW_QUEUE,
        )
        status = await until(handle, lambda s: s["actions"]["rollback"])
        assert {k: v for k, v in status["actions"].items() if k != "advance"} == {
            "rollback": True,
            "retry": False,
            "finalize": False,
        }  # advance(건너뛰기)는 관찰 창이 열린 동안만
        await handle.signal(TrafficShiftWorkflow.request_retry)  # wrong state: ignored

        await handle.signal(TrafficShiftWorkflow.request_rollback)
        status = await until(handle, lambda s: s["actions"]["retry"])
        assert status["snapshot"]["phase"] == "rolledback"
        assert status["snapshot"]["weights"] == {"origin": 100, "target": 0}
        assert status["actions"] == {
            "rollback": False,
            "retry": True,
            "finalize": False,
            "advance": False,
        }
        await handle.signal(TrafficShiftWorkflow.request_rollback)  # already rolled back: ignored

        relay.release.set()
        await handle.signal(TrafficShiftWorkflow.request_retry)
        result = await handle.result()
        history = await handle.fetch_history()

    assert result["status"] == "DONE"
    assert [v for kind, v in relay.calls if kind == "set"] == [10, 10, 50, 100]
    assert relay.calls.count(("rollback", None)) == 1
    snapshots = store.of("traffic.snapshot")
    manual = next(e for e in snapshots if e["data"]["phase"] == "rolledback")
    assert manual["status"] == "running"  # a retry is still possible: not a migration failure
    assert manual["data"]["notes"] == [
        {"key": "manual", "activity": "traffic_rollback", "params": {"origin": 100, "target": 0}}
    ]
    restart = snapshots[snapshots.index(manual) + 1]
    assert restart["data"]["phase"] == "applying" and restart["data"]["notes"] == [
        {"key": "restart"}
    ]
    assert snapshots[-1]["status"] == "done"
    await Replayer(workflows=[TrafficShiftWorkflow]).replay_workflow(history)


async def test_cancel_while_waiting_for_a_retry_ends_without_another_rollback() -> None:
    store, relay = Store(READINESS), Relay([BAD])
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[TrafficShiftWorkflow],
            activities=store.activities(),
        ),
        relay.worker(env.client),
    ):
        handle = await env.client.start_workflow(
            TrafficShiftWorkflow.run,
            body(),
            id=f"traffic-{uuid.uuid4()}",
            task_queue=WORKFLOW_QUEUE,
        )
        await until(handle, lambda s: s["actions"]["retry"])
        await handle.signal(TrafficShiftWorkflow.request_cancel)
        result = await handle.result()
    assert result["status"] == "ROLLED_BACK"
    assert relay.calls.count(("rollback", None)) == 1


@pytest.mark.parametrize("phase", ["prepare", "complete"])
def test_rollback_is_offered_only_while_shifting(phase) -> None:
    flow = TrafficShiftWorkflow()
    flow.snapshot = {"phase": phase} if phase != "prepare" else {}
    assert flow.can_rollback() is False
    flow.snapshot = {"phase": "observing"}
    assert flow.can_rollback() is True


# ---- Workflow: 100% 뒤 안정화 구간(장애 → 알림 → 사람의 롤백 → DB까지 되돌림 → 재전환 → 확정) ----

STABILIZE = {**POLICY, "stabilize_seconds": 600, "stabilize_window_seconds": 1}
FAULTY = {"requests": 100, "failed": 40, "p99_ms": 900.0}


@workflow.defn(name="DataMigrationWorkflow")
class FakeData:
    """데이터 이전 자식 흉내: 롤백·재전환 신호를 기록하고 DB 게이트를 돌려준다."""

    def __init__(self) -> None:
        self.signals: list[str] = []
        self.writer = "target"
        self.done = False

    @workflow.signal
    def rollback(self) -> None:
        self.signals.append("rollback")
        self.writer = "source"

    @workflow.signal
    def switchover(self) -> None:
        self.signals.append("switchover")
        self.writer = "target"

    @workflow.signal
    def finish(self) -> None:
        self.done = True

    @workflow.query
    def seen(self) -> list[str]:
        return self.signals

    @workflow.query
    def db_gate(self) -> dict:
        return {"writer": self.writer, "reverse_replication_ok": self.writer == "target"}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.done)


@workflow.defn(name="DataMigrationWorkflow")
class FakeDataOnSource(FakeData):
    """DB를 아직 전환하지 않은 데이터 이전: 쓰기 주체가 원본이다."""

    def __init__(self) -> None:
        super().__init__()
        self.writer = "source"

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.done)


def stabilize_workers(env, store: Store, relay: Relay, data=FakeData):
    return (
        Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[TrafficShiftWorkflow, data],
            activities=[*store.activities(), traffic_activities.traffic_db_gate],
        ),
        relay.worker(env.client),
    )


async def test_stabilizing_after_100_percent_waits_for_a_person_to_finalize() -> None:
    store, relay = Store(READINESS), Relay(pace=0.05)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        main, relay_worker = stabilize_workers(env, store, relay)
        async with main, relay_worker:
            handle = await env.client.start_workflow(
                TrafficShiftWorkflow.run,
                body(policy=STABILIZE),
                id=f"traffic-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )
            status = await until(handle, lambda s: s["actions"]["finalize"])
            assert status["snapshot"]["phase"] == "stabilizing"
            assert status["snapshot"]["weights"] == {"origin": 0, "target": 100}
            assert status["actions"]["rollback"] is True
            await handle.signal(TrafficShiftWorkflow.request_finalize)
            result = await handle.result()
            history = await handle.fetch_history()

    assert result["status"] == "DONE"
    assert result["events"][-1] == {"step": 100, "decision": "finalize", "reason": "manual"}
    last = store.of("traffic.snapshot")[-1]
    assert last["status"] == "done" and last["data"]["notes"] == [
        {"key": "finalized", "params": {"by": "manual"}}
    ]
    assert store.of("traffic.alert") == []  # 정상이면 알리지 않는다
    await Replayer(workflows=[TrafficShiftWorkflow]).replay_workflow(history)


async def test_a_fault_while_stabilizing_alerts_then_rollback_returns_traffic_and_db() -> None:
    windows = [GOOD] * 3 + [FAULTY] * 200  # 단계 3개는 정상, 100% 뒤부터 대상이 오류를 낸다
    store, relay = Store(READINESS), Relay(windows, pace=0.05)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        main, relay_worker = stabilize_workers(env, store, relay)
        async with main, relay_worker:
            data = await env.client.start_workflow(
                FakeData.run, id="data-migration-1", task_queue=WORKFLOW_QUEUE
            )
            handle = await env.client.start_workflow(
                TrafficShiftWorkflow.run,
                body(policy=STABILIZE, retry_wait_seconds=600),
                id=f"traffic-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )
            status = await until(
                handle, lambda s: (s["snapshot"].get("health") or {}).get("breach")
            )
            assert status["snapshot"]["phase"] == "stabilizing"
            assert relay.calls.count(("rollback", None)) == 0  # 자동으로 되돌리지 않는다

            await handle.signal(TrafficShiftWorkflow.request_rollback)
            status = await until(handle, lambda s: s["actions"]["retry"])
            assert status["snapshot"]["weights"] == {"origin": 100, "target": 0}
            assert await data.query(FakeData.seen) == ["rollback"]

            relay.windows = []  # 장애를 껐다: 이제 정상
            await handle.signal(TrafficShiftWorkflow.request_retry)
            await until(handle, lambda s: s["actions"]["finalize"])
            assert await data.query(FakeData.seen) == ["rollback", "switchover"]
            await handle.signal(TrafficShiftWorkflow.request_finalize)
            result = await handle.result()
            history = await handle.fetch_history()
            await data.signal(FakeData.finish)

    assert result["status"] == "DONE"
    assert [v for kind, v in relay.calls if kind == "set"] == [10, 50, 100, 10, 50, 100]
    assert relay.calls.count(("rollback", None)) == 1
    (alert,) = [a for a in store.of("traffic.alert") if a["data"]["state"] == "breach"]
    assert alert["data"]["error_rate"] == 0.4
    assert {b["code"] for b in alert["data"]["breach"]} == {"ERROR_RATE", "P99"}
    rolled = next(e for e in store.of("traffic.snapshot") if e["data"]["phase"] == "rolledback")
    assert rolled["status"] == "running"  # 재시도할 수 있으니 이전 실패가 아니다
    db_back = [
        e
        for e in store.of("traffic.snapshot")
        if {"key": "dbRollback"} in (e["data"].get("notes") or [])
    ]
    assert [e["data"]["phase"] for e in db_back] == ["rolledback"]
    await Replayer(workflows=[TrafficShiftWorkflow]).replay_workflow(history)


async def test_without_stabilize_seconds_the_shift_ends_at_100_percent_as_before() -> None:
    store = Store(READINESS)
    result = await run_to_end(Relay([GOOD] * 3), store, body())
    assert result["status"] == "DONE"
    assert all(e["data"]["phase"] != "stabilizing" for e in store.of("traffic.snapshot"))


SHORT = {**STABILIZE, "stabilize_seconds": 3, "stabilize_hold_seconds": 3}
IDLE = {"requests": 5, "failed": 0, "p99_ms": 50.0}


async def run_stabilizing(windows, policy, data=None) -> tuple[dict, Store, list[str]]:
    store, relay = Store(READINESS), Relay([GOOD] * 3 + windows, pace=0.01)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        main, relay_worker = stabilize_workers(env, store, relay, data or FakeData)
        async with main, relay_worker:
            child = None
            if data is not None:
                child = await env.client.start_workflow(
                    data.run, id="data-migration-1", task_queue=WORKFLOW_QUEUE
                )
            handle = await env.client.start_workflow(
                TrafficShiftWorkflow.run,
                body(policy=policy, retry_wait_seconds=1),
                id=f"traffic-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )
            if data is not None:
                await until(handle, lambda s: (s["snapshot"].get("health") or {}).get("breach"))
                await handle.signal(TrafficShiftWorkflow.request_rollback)
            result = await handle.result()
            history = await handle.fetch_history()
            seen = await child.query(FakeData.seen) if child else []
            if child:
                await child.signal(FakeData.finish)
    await Replayer(workflows=[TrafficShiftWorkflow]).replay_workflow(history)
    return result, store, seen


async def test_window_end_while_alerting_does_not_finalize_and_falls_back_to_origin() -> None:
    result, store, _ = await run_stabilizing([FAULTY] * 2000, SHORT)
    assert result["status"] == "ROLLED_BACK"
    assert "결정 없음" in result["events"][-1]["reason"]
    assert not any(e.get("decision") == "finalize" for e in result["events"])
    assert all(e["data"]["phase"] != "complete" for e in store.of("traffic.snapshot")[-5:])


async def test_window_end_without_any_judged_window_waits_then_finalizes_as_no_traffic() -> None:
    result, store, _ = await run_stabilizing([IDLE] * 2000, SHORT)
    assert result["status"] == "DONE"
    assert result["events"][-1] == {"step": 100, "decision": "finalize", "reason": "no_traffic"}
    stabilizing = [e for e in store.of("traffic.snapshot") if e["data"]["phase"] == "stabilizing"]
    assert any(
        e["data"]["stabilize_left_seconds"] == 0 for e in stabilizing
    )  # 구간 뒤에도 기다렸다
    assert store.of("traffic.alert") == []


async def test_stabilizing_rollback_leaves_the_db_alone_when_it_was_never_switched() -> None:
    policy = {**STABILIZE, "stabilize_seconds": 60}
    result, store, seen = await run_stabilizing([FAULTY] * 2000, policy, FakeDataOnSource)
    assert result["status"] == "ROLLED_BACK"
    assert seen == []  # 쓰기 주체가 원본이면 롤백 신호를 보내지 않는다
    assert not any(
        {"key": "dbRollback"} in (e["data"].get("notes") or [])
        for e in store.of("traffic.snapshot")
    )
