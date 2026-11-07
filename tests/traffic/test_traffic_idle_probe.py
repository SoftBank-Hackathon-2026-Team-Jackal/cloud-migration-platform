"""사용자 트래픽이 없을 때(원본·대상 모두 요청이 거의 없을 때)의 합성 요청 판정.

롤백하지 않고 대상으로 합성 요청을 보내 평소와 같은 기준(실패·p99)으로 판정한다. 원본에 사용자 요청이 있는데
대상만 0건이면 사용자 없음이 아니므로 합성 요청을 보내지 않는다. 판정은 순수 함수로, 흐름은 Temporal 위에서 본다.
"""

from __future__ import annotations

import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from cmp_contracts.events import validate_data
from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from cmp_traffic import (
    Sample,
    TrafficPolicy,
    evaluate_step,
    is_idle,
    probe_count,
    summarize_probe,
)
from cmp_traffic.envoy import EnvoyObserver
from cmp_traffic.probe import MAX_PROBE_REQUESTS, percentile
from cmp_worker.activities.traffic import traffic_probe
from cmp_worker.workflows.traffic import TrafficShiftWorkflow
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

# ---- 순수 판정 ----


def test_percentile_uses_nearest_rank_and_is_zero_without_values() -> None:
    assert percentile([], 99) == 0.0
    assert percentile([10.0], 99) == 10.0
    assert percentile([5, 1, 3, 2, 4], 50) == 3.0
    assert percentile(list(range(1, 101)), 99) == 99.0


def test_idle_means_both_sides_are_below_the_minimum() -> None:
    assert is_idle(origin_requests=0, target_requests=0, min_requests=10) is True
    assert is_idle(origin_requests=4, target_requests=3, min_requests=10) is True
    # 원본에 사용자 요청이 있으면(대상만 0건) 사용자 없음이 아니다: 가중치가 안 먹은 것일 수 있다.
    assert is_idle(origin_requests=500, target_requests=0, min_requests=10) is False
    assert is_idle(origin_requests=0, target_requests=10, min_requests=10) is False


def test_idle_is_never_assumed_when_the_origin_count_is_unknown() -> None:
    assert is_idle(origin_requests=None, target_requests=0, min_requests=10) is False


@pytest.mark.parametrize(
    ("target", "minimum", "expected"),
    [(0, 30, 30), (7, 30, 23), (30, 30, 1), (0, 5000, MAX_PROBE_REQUESTS)],
)
def test_probe_count_fills_up_to_the_minimum_within_the_cap(target, minimum, expected) -> None:
    assert probe_count(target, minimum) == expected


def test_summary_counts_connect_errors_and_5xx_as_failures_but_not_4xx() -> None:
    sample = summarize_probe([(200, 20.0), (404, 30.0), (503, 50.0), (None, 3000.0)])
    assert sample == Sample(requests=4, failed=2, p99_ms=3000.0)


def test_policy_enables_the_probe_by_default_and_can_turn_it_off() -> None:
    assert TrafficPolicy().idle_probe is True
    assert TrafficPolicy(idle_probe=False).idle_probe is False


def test_rollback_decision_carries_machine_readable_detail() -> None:
    policy = TrafficPolicy(p99_floor_ms=500)
    base = Sample(requests=0, failed=0, p99_ms=0)
    slow = Sample(requests=100, failed=0, p99_ms=1000)
    out = evaluate_step(policy, base, slow, elapsed_s=1, p99_streak=2)
    assert out.decision == "rollback"
    assert out.detail == {"kind": "p99", "p99_ms": 1000, "limit_ms": 500, "baseline_ms": 0}
    failing = evaluate_step(policy, base, Sample(requests=100, failed=3, p99_ms=1), elapsed_s=1)
    assert failing.detail == {"kind": "failed", "failed": 3, "allowed": 0}
    thin = evaluate_step(policy, base, Sample(requests=2, failed=0, p99_ms=1), elapsed_s=99999)
    assert thin.detail["kind"] == "requests" and thin.detail["min_requests"] == policy.min_requests


# ---- 관측기: 한 창에서 원본·대상을 함께 ----


def cluster_payload(side: str, requests: int) -> list[dict]:
    prefix = f"cluster.cluster_{side}."
    return [
        {"name": prefix + "upstream_rq_total", "value": requests},
        {"name": prefix + "upstream_rq_5xx", "value": 0},
        {"name": prefix + "upstream_rq_timeout", "value": 0},
    ]


def test_observer_samples_origin_and_target_from_the_same_window() -> None:
    calls = iter(
        [
            {"stats": cluster_payload("origin", 100) + cluster_payload("target", 10)},
            {"stats": cluster_payload("origin", 160) + cluster_payload("target", 25)},
        ]
    )
    slept: list[float] = []
    observer = EnvoyObserver(fetch=lambda url: next(calls), sleep=slept.append)
    origin, target = observer.sample_pair(5)
    assert (origin.requests, target.requests) == (60, 15)
    assert slept == [5]  # 창은 한 번만 기다린다


# ---- Temporal 위의 흐름 ----

READY = {"status": "READY", "ready": True, "external_endpoint": "10.200.0.4", "port": 8080}
POLICY = {"observe_seconds": 1, "min_requests": 30, "p99_floor_ms": 500}
GOOD = {"requests": 100, "failed": 0, "p99_ms": 100.0, "origin_requests": 100}
IDLE = {"requests": 0, "failed": 0, "p99_ms": 0.0, "origin_requests": 0}
PROBE_OK = {"requests": 30, "failed": 0, "p99_ms": 120.0}


def worker(client, windows, probes, calls):
    seq, probe_seq = iter(windows), iter(probes)

    @activity.defn(name="traffic_prepare")
    async def prepare(payload: dict) -> dict:
        return {"baseline": {"requests": 0, "failed": 0, "p99_ms": 0.0}}

    @activity.defn(name="traffic_set_weight")
    async def set_weight(payload: dict) -> dict:
        calls.append(("set", payload["weights"]["target"]))
        return payload

    @activity.defn(name="traffic_observe")
    async def observe(payload: dict) -> dict:
        calls.append(("observe", None))
        return next(seq)

    @activity.defn(name="traffic_probe")
    async def probe(payload: dict) -> dict:
        calls.append(("probe", payload["count"]))
        result = next(probe_seq)
        if isinstance(result, Exception):
            raise result
        return result

    @activity.defn(name="traffic_rollback")
    async def rollback(payload: dict) -> dict:
        calls.append(("rollback", None))
        return {}

    return Worker(
        client,
        task_queue=MIGRATION_QUEUE,
        activities=[prepare, set_weight, observe, probe, rollback],
    )


async def run(windows, probes=(), policy=None, *, events=None):
    calls: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:

        @activity.defn(name="emit_migration_event")
        async def emit(request: dict) -> int:
            validate_data(request["type"], request["data"])  # 웹 계약이 받아들이는 모양인지
            if events is not None:
                events.append(request)
            return 1

        flow = Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[TrafficShiftWorkflow],
            activities=[emit],
        )
        async with flow, worker(env.client, windows, probes, calls):
            body = {"handover": READY, "policy": {**POLICY, **(policy or {})}}
            if events is not None:
                body["migration_id"] = "m-1"
            handle = await env.client.start_workflow(
                TrafficShiftWorkflow.run,
                body,
                id=f"traffic-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )
            result = await handle.result()
            status = await handle.query(TrafficShiftWorkflow.status)
            history = await handle.fetch_history()
    return result, calls, status, history


@pytest.mark.asyncio
async def test_no_user_traffic_is_verified_by_probe_and_the_shift_finishes() -> None:
    result, calls, _, _ = await run([IDLE, IDLE, IDLE], [PROBE_OK, PROBE_OK, PROBE_OK])
    assert result["status"] == "DONE"
    assert result["final_weights"] == {"origin": 0, "target": 100}
    assert [c for c in calls if c[0] == "probe"] == [("probe", 30)] * 3
    assert "rollback" not in [c[0] for c in calls]


@pytest.mark.asyncio
async def test_idle_probe_note_reaches_the_web_event_with_the_numbers() -> None:
    events: list[dict] = []
    await run([IDLE, IDLE, IDLE], [PROBE_OK, PROBE_OK, PROBE_OK], events=events)
    notes = [
        note
        for event in events
        if event["type"] == "traffic.snapshot"
        for note in event["data"].get("notes", [])
        if note["key"] == "idleProbe"
    ]
    assert len(notes) == 3  # 단계마다 한 번
    assert notes[0]["params"] == {"requests": 30, "failed": 0, "p99_ms": 120}


@pytest.mark.asyncio
async def test_rollback_reason_reaches_the_web_event() -> None:
    events: list[dict] = []
    slow = {"requests": 30, "failed": 0, "p99_ms": 1000.0}
    await run([IDLE, IDLE], [slow, slow], events=events)
    fail = [
        note
        for event in events
        if event["type"] == "traffic.snapshot"
        for note in event["data"].get("notes", [])
        if note["key"] == "gateFail"
    ]
    assert fail and fail[-1]["params"]["kind"] == "p99"


@pytest.mark.asyncio
async def test_a_failing_probe_rolls_back_like_failed_user_requests() -> None:
    bad = {"requests": 30, "failed": 4, "p99_ms": 90.0}
    result, calls, status, _ = await run([IDLE], [bad])
    assert result["status"] == "ROLLED_BACK"
    assert calls[-1][0] == "rollback"
    note = status["snapshot"]["notes"][0]
    assert note["key"] == "gateFail"
    assert note["params"] == {"kind": "failed", "failed": 4, "allowed": 0}


@pytest.mark.asyncio
async def test_slow_probe_needs_two_windows_then_rolls_back_with_the_reason() -> None:
    slow = {"requests": 30, "failed": 0, "p99_ms": 1000.0}
    result, calls, status, _ = await run([IDLE, IDLE], [slow, slow])
    assert result["status"] == "ROLLED_BACK"
    assert [c[0] for c in calls].count("probe") == 2
    note = status["snapshot"]["notes"][0]
    assert note["params"]["kind"] == "p99"
    assert note["params"]["p99_ms"] == 1000 and note["params"]["limit_ms"] == 500


@pytest.mark.asyncio
async def test_origin_traffic_without_target_traffic_is_not_idle_and_never_probes() -> None:
    # 원본엔 사용자 요청이 있는데 대상은 0건: 가중치가 안 먹었을 수 있으니 합성 요청으로 덮지 않는다.
    thin_target = {"requests": 0, "failed": 0, "p99_ms": 0.0, "origin_requests": 500}
    result, calls, _, _ = await run([thin_target, GOOD, GOOD, GOOD], [])
    assert result["status"] == "DONE"
    assert "probe" not in [c[0] for c in calls]


@pytest.mark.asyncio
async def test_probe_can_be_turned_off_by_policy() -> None:
    result, calls, _, _ = await run([IDLE, GOOD, GOOD, GOOD], [], {"idle_probe": False})
    assert result["status"] == "DONE"
    assert "probe" not in [c[0] for c in calls]


@pytest.mark.asyncio
async def test_old_observe_results_without_the_origin_count_never_probe() -> None:
    old = {"requests": 0, "failed": 0, "p99_ms": 0.0}
    result, calls, _, _ = await run([old, GOOD, GOOD, GOOD], [])
    assert result["status"] == "DONE"
    assert "probe" not in [c[0] for c in calls]


@pytest.mark.asyncio
async def test_a_probe_that_cannot_run_falls_back_to_holding_like_before() -> None:
    boom = ApplicationError("not allowed", type="TargetMismatch", non_retryable=True)
    result, calls, _, _ = await run([IDLE, GOOD, GOOD, GOOD], [boom])
    assert result["status"] == "DONE"
    assert [c[0] for c in calls].count("probe") == 1


@pytest.mark.asyncio
async def test_history_with_probes_replays_without_nondeterminism() -> None:
    from temporalio.worker import Replayer

    _, _, _, history = await run([IDLE, IDLE, IDLE], [PROBE_OK, PROBE_OK, PROBE_OK])
    await Replayer(workflows=[TrafficShiftWorkflow]).replay_workflow(history)


# ---- 합성 요청 Activity(실제 HTTP 서버에 보낸다) ----


@pytest.fixture
def target_server():
    class Handler(BaseHTTPRequestHandler):
        status = 200
        paths: ClassVar[list[str]] = []

        def do_GET(self):
            type(self).paths.append(self.path)
            self.send_response(type(self).status)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, Handler
    server.shutdown()


def probe_payload(port: int, count: int, host: str = "127.0.0.1") -> dict:
    handover = {
        "status": "READY",
        "ready": True,
        "external_endpoint": f"http://{host}:{port}",
        "port": port,
        "health_path": "/healthz",
    }
    return {"handover": handover, "count": count, "timeout_s": 2}


@pytest.mark.asyncio
async def test_probe_activity_sends_read_only_gets_and_summarizes(
    monkeypatch, tmp_path, target_server
) -> None:
    monkeypatch.setenv("TRAFFIC_RDS_PATH", str(tmp_path / "rds.json"))
    monkeypatch.setenv("TRAFFIC_TARGET_ALLOWED_CIDRS", "127.0.0.0/8")
    server, handler = target_server
    result = await ActivityEnvironment().run(traffic_probe, probe_payload(server.server_port, 12))
    assert result["requests"] == 12 and result["failed"] == 0
    assert handler.paths == ["/healthz"] * 12  # 인계값의 헬스 경로로만


@pytest.mark.asyncio
async def test_probe_activity_counts_5xx_as_failures(monkeypatch, tmp_path, target_server) -> None:
    monkeypatch.setenv("TRAFFIC_RDS_PATH", str(tmp_path / "rds.json"))
    monkeypatch.setenv("TRAFFIC_TARGET_ALLOWED_CIDRS", "127.0.0.0/8")
    server, handler = target_server
    handler.status = 503
    result = await ActivityEnvironment().run(traffic_probe, probe_payload(server.server_port, 6))
    assert result["requests"] == 6 and result["failed"] == 6


@pytest.mark.asyncio
async def test_probe_activity_refuses_targets_outside_the_allowed_range(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("TRAFFIC_RDS_PATH", str(tmp_path / "rds.json"))
    monkeypatch.setenv("TRAFFIC_ENVOY_ADMIN", "http://127.0.0.1:9")  # 닿지 않는 관리 주소
    with pytest.raises(ApplicationError) as error:
        await ActivityEnvironment().run(traffic_probe, probe_payload(8080, 3, "203.0.113.9"))
    assert error.value.type == "TargetMismatch"
