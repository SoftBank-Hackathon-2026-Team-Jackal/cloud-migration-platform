"""#128: 부모 MigrationWorkflow의 traffic 단계가 TrafficShiftWorkflow(자식)를 실제로 시작하는지 시험.

MigrationWorkflow를 상속한 시험용 Workflow로 `_shift_traffic`만 실행한다. 중계 서버 Activity는 가짜,
자식 TrafficShiftWorkflow는 진짜다. 추가로 Envoy 대상 주소 확인(순수 함수)과 시작 입력을 시험한다.
"""

from __future__ import annotations

import json
import uuid

import pytest
from cmp_contracts.events import TrafficSnapshotData
from cmp_contracts.queues import WORKFLOW_QUEUE
from cmp_traffic import TrafficShiftInput
from cmp_traffic.envoy import EnvoyObserver, cluster_target_address
from cmp_worker.activities.traffic import handover_target
from cmp_worker.start_input import build_start_input
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.traffic import TrafficShiftWorkflow
from pydantic import ValidationError
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

HANDOFF = {
    "plan_id": "plan-1",
    "revision": 2,
    "proceed": True,
    "handover": {
        "status": "READY",
        "ready": True,
        "external_endpoint": "http://10.200.0.3:8080",
        "health_path": "/healthz",
        "port": 8080,
    },
}
EVENTS: list[dict] = []  # what the fake `emit_migration_event` stored in the last run
GOOD = {"requests": 100, "failed": 0, "p99_ms": 100.0}
GIVEN = {"relay_queue": "relay-test", "policy": {"observe_seconds": 1, "min_requests": 30}}


# ---- 순수: Envoy 대상 주소와 인계값 ----


def dump(address: str, port: int) -> dict:
    cluster = {
        "name": "cluster_target",
        "load_assignment": {
            "endpoints": [
                {
                    "lb_endpoints": [
                        {
                            "endpoint": {
                                "address": {
                                    "socket_address": {"address": address, "port_value": port}
                                }
                            }
                        }
                    ]
                }
            ]
        },
    }
    origin = {"name": "cluster_origin"}
    return {"configs": [{"static_clusters": [{"cluster": origin}, {"cluster": cluster}]}]}


def test_envoy_target_address_is_read_from_the_static_cluster() -> None:
    assert cluster_target_address(dump("10.200.0.3", 8080)) == ("10.200.0.3", 8080)
    assert cluster_target_address({"configs": [{"static_clusters": []}]}) is None
    assert cluster_target_address({}) is None


def test_observer_reports_none_when_the_admin_api_is_unreachable() -> None:
    def broken(url: str) -> dict:
        raise OSError("down")

    assert EnvoyObserver(fetch=broken).target_address() is None
    assert EnvoyObserver(fetch=lambda url: dump("203.0.113.9", 80)).target_address() == (
        "203.0.113.9",
        80,
    )


def test_handover_target_is_the_host_and_port_the_relay_must_point_at() -> None:
    from cmp_traffic import Handover

    ready = Handover.model_validate(HANDOFF["handover"])
    assert handover_target(ready) == ("10.200.0.3", 8080)
    named = Handover(
        status="READY", ready=True, external_endpoint="http://lb.example:9000", port=9000
    )
    assert handover_target(named) == ("lb.example", 9000)
    assert handover_target(Handover(status="READY", ready=True, external_endpoint=None)) is None


# ---- 시작 입력 ----


def test_start_input_carries_validated_traffic_settings() -> None:
    data = build_start_input(
        project_id="p", repository="r", commit="c", traffic_json=json.dumps(GIVEN)
    )
    assert data["traffic"]["relay_queue"] == "relay-test"
    assert data["traffic"]["policy"]["observe_seconds"] == 1
    assert "traffic" not in build_start_input(project_id="p", repository="r", commit="c")
    with pytest.raises(ValidationError):
        build_start_input(
            project_id="p", repository="r", commit="c",
            traffic_json=json.dumps({"relay_queue": "bad queue name"}),
        )  # fmt: skip
    assert TrafficShiftInput().relay_queue is None


# ---- 부모 단계 + 진짜 자식 ----


@workflow.defn(name="TrafficStepProbe")
class TrafficStepProbe(MigrationWorkflow):
    @workflow.run
    async def run(self, given: dict) -> str:
        self.traffic_handoff = HANDOFF
        return (await self._shift_traffic(given)) or "continue"


def relay_worker(client, calls, *, observe=None, apply_fails=False):
    windows = iter(observe or [GOOD] * 8)

    @activity.defn(name="traffic_prepare")
    async def prepare(payload: dict) -> dict:
        calls.append(("prepare", payload["handover"]["external_endpoint"]))
        return {"baseline": GOOD}

    @activity.defn(name="traffic_set_weight")
    async def set_weight(payload: dict) -> dict:
        calls.append(("set", payload["weights"]["target"]))
        if apply_fails:
            raise ApplicationError("not confirmed", non_retryable=True)
        return payload

    @activity.defn(name="traffic_observe")
    async def observe_window(payload: dict) -> dict:
        return next(windows)

    @activity.defn(name="traffic_rollback")
    async def rollback(payload: dict) -> dict:
        calls.append(("rollback", None))
        return {}

    return Worker(
        client,
        task_queue=GIVEN["relay_queue"],
        activities=[prepare, set_weight, observe_window, rollback],
    )


@activity.defn(name="emit_migration_event")
async def fake_emit(request: dict) -> int:
    EVENTS.append(request)
    return len(EVENTS)


async def run_probe(given=None, **kw):
    calls: list = []
    EVENTS.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[TrafficStepProbe, TrafficShiftWorkflow],
            activities=[fake_emit],
        ),
        relay_worker(env.client, calls, **kw),
    ):
        pid = f"migration-{uuid.uuid4()}"
        handle = await env.client.start_workflow(
            TrafficStepProbe.run, given or GIVEN, id=pid, task_queue=WORKFLOW_QUEUE
        )
        return await handle.result(), await handle.query("status"), calls, pid


async def test_a_finished_shift_lets_the_run_go_on_to_verification() -> None:
    result, status, calls, _ = await run_probe()
    assert result == "continue"
    assert status["traffic_shift"] == {
        "status": "DONE",
        "final_weights": {"origin": 0, "target": 100},
    }
    # 인계값이 그대로 자식에게 갔고 10→50→100으로 올렸다.
    assert calls[0] == ("prepare", "http://10.200.0.3:8080")
    assert [value for kind, value in calls if kind == "set"] == [10, 50, 100]
    assert ("rollback", None) not in calls


async def test_gates_that_roll_back_end_the_run_as_traffic_rolled_back() -> None:
    bad = {"requests": 100, "failed": 5, "p99_ms": 100.0}
    result, status, calls, _ = await run_probe(observe=[bad])
    assert result == "traffic_rolled_back"
    assert status["failure_code"] == "TRAFFIC_ROLLED_BACK"
    assert status["traffic_shift"]["final_weights"] == {"origin": 100, "target": 0}
    assert ("rollback", None) in calls


async def test_an_apply_that_is_not_confirmed_rolls_back_too() -> None:
    result, status, _calls, _ = await run_probe(apply_fails=True)
    assert result == "traffic_rolled_back"
    assert status["traffic_shift"]["status"] == "ROLLED_BACK"


async def test_every_snapshot_is_stored_as_a_valid_web_event_under_the_migration_id() -> None:
    _result, _status, _calls, pid = await run_probe()
    assert EVENTS and {event["migration_id"] for event in EVENTS} == {pid}
    assert {(event["type"], event["phase"]) for event in EVENTS} == {
        ("traffic.snapshot", "traffic")
    }
    for event in EVENTS:
        TrafficSnapshotData.model_validate(event["data"])  # the web contract accepts every payload
    phases = [event["data"]["phase"] for event in EVENTS]
    assert phases[0] == "applying" and phases[-1] == "complete"
    assert "observing" in phases and "gate" in phases
    assert [event["status"] for event in EVENTS][-1] == "done"
    assert {event["status"] for event in EVENTS[:-1]} == {"running"}
    assert EVENTS[-1]["data"]["weights"] == {"origin": 0, "target": 100}


async def test_a_rollback_is_stored_as_a_failed_snapshot_with_the_source_weights() -> None:
    bad = {"requests": 100, "failed": 5, "p99_ms": 100.0}
    await run_probe(observe=[bad])
    last = EVENTS[-1]
    assert last["data"]["phase"] == "rolledback" and last["status"] == "failed"
    assert last["data"]["weights"] == {"origin": 100, "target": 0}
    TrafficSnapshotData.model_validate(last["data"])
