"""Ticket 62: `GET /migrations/{id}/report` from stored migration events."""

from datetime import UTC, datetime, timedelta

import pytest
from cmp_api import migration_events
from cmp_api.db import Base
from cmp_api.event_stream import StatusUnavailable
from cmp_api.main import app
from cmp_api.migration_events import DbEventEmitter
from cmp_api.routes.migrations import get_event_sessions, get_status_reader
from cmp_data import cutover_events
from cmp_data.contracts import ReadinessPolicy
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

MID = "migration-report"
T0 = datetime(2026, 11, 7, 1, 0, 0, tzinfo=UTC)
SECRET = "postgres://app:hunter2-secret@db:5432/app"


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'report.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.now.microsecond // 1000:03d}Z"


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(migration_events, "utc_now", value)
    return value


class Run:
    def __init__(self, sessions, clock):
        self.emitter = DbEventEmitter(sessions)
        self.clock = clock
        self.n = 0

    def at(self, seconds: float, event_type, phase, status, data):
        self.clock.now = T0 + timedelta(seconds=seconds)
        self.n += 1
        stored = self.emitter.emit(
            MID, event_type, phase, status, data, idempotency_key=f"k{self.n}"
        )
        assert stored is not None, (event_type, data)


FINDING = {
    "id": "f1",
    "stream": "infrastructure",
    "key": "eks",
    "via": ["config"],
    "progress": 50,
    "message": "",
    "detail": "",
}
FIX = {"id": "fix-1", "check": "build", "lines": [], "why": "", "evidence": ""}
ATTEMPT = {
    "run": 1,
    "attempt": 2,
    "max_attempts": 3,
    "verdict": {"passed": True, "reasons": []},
    "failure_class": None,
    "checks": {
        "required_passed": 3,
        "required_total": 3,
        "unsupported_calls": 0,
        "calls_in_mapping": 0,
        "calls_total": 0,
        "blocked_egress": 0,
    },
    "fix_ids": ["fix-1", "fix-2"],
}
DECISION = {
    "approval_id": "a1",
    "plan_id": "p1",
    "revision": 1,
    "decision": "APPROVE",
    "scope": "INFRASTRUCTURE",
    "delivery_status": "DELIVERED",
    "delivery_attempts": 1,
    "execution_status": "RUNNING",
}


def resource(rid, state):
    return {"id": rid, "key": "vpc", "state": state, "progress": 100 if state == "done" else 10}


def through_approval(run: Run):
    run.at(0, "analysis.finding", "analysis", "running", FINDING)
    run.at(42, "analysis.done", "analysis", "done", {})
    run.at(45, "build.fix", "build", "running", FIX)
    run.at(50, "sandbox.attempt", "build", "running", ATTEMPT)
    run.at(
        63,
        "sandbox.done",
        "build",
        "done",
        {"status": "PASSED", "cleanup_status": "CLEANED", "digest": None},
    )
    run.at(70, "plan.step", "planning", "running", PLAN_STEP)
    run.at(79, "plan.ready", "planning", "done", PLAN_READY)
    run.at(80, "approval.required", "awaiting_approval", "waiting", APPROVAL_REQUIRED)
    run.at(110, "approval.decision", "awaiting_approval", "done", DECISION)


def client_with(sessions, state):
    async def read(_):
        if isinstance(state, Exception):
            raise state
        return state

    app.dependency_overrides[get_event_sessions] = lambda: sessions
    app.dependency_overrides[get_status_reader] = lambda: read
    return TestClient(app)


def phase(name, **extra):
    return {"phase": name, "approval_pending": False, "cancel_requested": False, **extra}


def stage_ids(body):
    return [stage["id"] for stage in body["stages"]]


def test_unknown_migration_is_404_with_code_only(sessions):
    response = client_with(sessions, None).get(f"/migrations/{MID}/report")
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "MIGRATION_NOT_FOUND"}}


def test_temporal_unavailable_without_events_is_an_empty_idle_report(sessions):
    response = client_with(sessions, StatusUnavailable()).get(f"/migrations/{MID}/report")
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["stages"], body["total_seconds"]) == ("idle", [], 0)
    assert body["current_stage"] is None


def test_in_progress_report_fills_only_finished_stages(sessions, clock):
    run = Run(sessions, clock)
    through_approval(run)
    run.at(120, "provision.resource", "provisioning", "running", resource("r1", "running"))
    body = client_with(sessions, phase("provisioning")).get(f"/migrations/{MID}/report").json()
    assert body["migration_id"] == MID
    assert body["status"] == "running"
    assert stage_ids(body) == ["analysis", "rehearsal", "planning", "approval"]
    analysis, rehearsal, planning, approval = body["stages"]
    assert analysis == {
        "id": "analysis",
        "phase": "analysis",
        "status": "done",
        "started_at": "2026-11-07T01:00:00.000Z",
        "ended_at": "2026-11-07T01:00:42.000Z",
        "seconds": 42,
    }
    assert (rehearsal["seconds"], planning["seconds"], approval["seconds"]) == (18, 9, 30)
    assert body["current_stage"] == "provisioning"
    assert body["total_seconds"] == 110  # wall clock: first start -> last finished end
    assert body["ai_fixes"] == 2  # build.fix id + sandbox.attempt fix_ids, deduplicated
    assert body["results"]["findings"] == 1
    assert body["results"]["sandbox"] == {"status": "PASSED", "attempts": 2}
    assert body["results"]["approval"] == "APPROVE"


def test_waiting_for_approval_keeps_the_approval_stage_open(sessions, clock):
    run = Run(sessions, clock)
    run.at(0, "analysis.done", "analysis", "done", {})
    run.at(10, "approval.required", "awaiting_approval", "waiting", APPROVAL_REQUIRED)
    body = client_with(sessions, phase("awaiting_approval")).get(f"/migrations/{MID}/report")
    body = body.json()
    assert body["status"] == "waiting"
    assert stage_ids(body) == ["analysis"]
    assert body["current_stage"] == "approval"


def test_done_report_has_every_stage_in_web_order_and_results(sessions, clock):
    run = Run(sessions, clock)
    through_approval(run)
    run.at(120, "provision.resource", "provisioning", "running", resource("r1", "running"))
    run.at(240, "provision.resource", "provisioning", "done", resource("r1", "done"))
    run.at(250, "data.verify", "data_copy", "running", {"name": "rows", "state": "running"})
    run.at(
        525,
        "data.verify",
        "data_copy",
        "done",
        {"name": "rows", "state": "done", "source": 10, "target": 10},
    )
    run.at(530, "traffic.ready", "traffic", "running", TRAFFIC_READY)
    run.at(610, "traffic.ready", "traffic", "done", TRAFFIC_READY)
    run.at(611, "cutover.step", "verification", "running", {"n": 1, "state": "running"})
    run.at(620, "cutover.result", "verification", "done", {"id": "pause", "value": "8", "ok": True})
    run.at(623, "cutover.handover", "verification", "done", {"item": "access", "state": "done"})
    body = client_with(sessions, phase("scaffold_complete")).get(f"/migrations/{MID}/report")
    body = body.json()
    assert body["status"] == "done"
    assert stage_ids(body) == [
        "analysis",
        "rehearsal",
        "planning",
        "approval",
        "provisioning",
        "data",
        "traffic",
        "cutover",
    ]
    assert [s["seconds"] for s in body["stages"]] == [42, 18, 9, 30, 120, 275, 80, 12]
    assert all(s["status"] == "done" for s in body["stages"])
    assert body["current_stage"] is None
    assert body["total_seconds"] == 623
    assert body["results"]["resources"] == {"done": 1, "failed": 0}
    assert body["results"]["data_verify"] == [
        {"name": "rows", "state": "done", "source": 10, "target": 10}
    ]
    assert body["results"]["cutover"] == [{"id": "pause", "value": "8", "ok": True}]
    assert body["results"]["error"] is None


def test_done_without_temporal_is_read_from_the_last_event(sessions, clock):
    run = Run(sessions, clock)
    run.at(0, "analysis.done", "analysis", "done", {})
    run.at(5, "cutover.handover", "verification", "done", {"item": "access", "state": "done"})
    response = client_with(sessions, StatusUnavailable()).get(f"/migrations/{MID}/report")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert stage_ids(body) == ["analysis", "cutover"]


def test_failed_report_marks_the_failed_stage_and_keeps_code_without_message(sessions, clock):
    run = Run(sessions, clock)
    run.at(0, "analysis.done", "analysis", "done", {})
    run.at(10, "provision.resource", "provisioning", "running", resource("r1", "running"))
    run.at(20, "provision.resource", "provisioning", "failed", resource("r1", "failed"))
    run.at(
        21,
        "error",
        "provisioning",
        "failed",
        {"code": "APPLY_FAILED", "message": SECRET, "phase": "provisioning", "retryable": False},
    )
    state = phase("provisioning_failed", execution_status="COMPLETED")
    response = client_with(sessions, state).get(f"/migrations/{MID}/report")
    body = response.json()
    assert body["status"] == "failed"
    assert stage_ids(body) == ["analysis", "provisioning"]
    assert body["stages"][1]["status"] == "failed"
    assert body["stages"][1]["seconds"] == 11
    assert body["results"]["error"] == {"code": "APPLY_FAILED", "phase": "provisioning"}
    assert body["results"]["resources"] == {"done": 0, "failed": 1}
    assert "hunter2" not in response.text
    assert "message" not in response.text


def test_report_never_copies_free_text_fields(sessions, clock):
    run = Run(sessions, clock)
    run.at(0, "analysis.finding", "analysis", "running", {**FINDING, "detail": SECRET})
    run.at(3, "analysis.done", "analysis", "done", {})
    response = client_with(sessions, phase("analysis")).get(f"/migrations/{MID}/report")
    assert response.status_code == 200
    assert "hunter2" not in response.text
    assert "postgres://" not in response.text


PLAN_STEP = {
    "id": "s1",
    "group": "infrastructure",
    "grade": "DIRECT",
    "before": "",
    "after": "",
    "evidence": "",
    "change": "",
    "action": "PROCEED",
    "keep_source": False,
    "verification": "SANDBOX_VERIFIED",
}
PLAN_READY = {"plan_id": "p1", "revision": 1}
APPROVAL_REQUIRED = {"excluded": [], "rollback_policy": "KEEP_SOURCE"}
TRAFFIC_READY = {"state": "running", "lag_ms": 0, "longest_tx_ms": 0, "commits_per_sec": 0}


OUTCOME_SWITCHED = {
    "result": "switched",
    "active": "target",
    "pause_ms": 110,
    "steps": [
        {"id": "pause", "ms": 3, "window": "pause"},
        {"id": "resume", "ms": 1, "window": "pause"},
    ],
    "replication": {"direction": "reverse", "lag_ms": None},
}


def report_with_outcomes(sessions, clock, *outcomes):
    run = Run(sessions, clock)
    run.at(0, "analysis.done", "analysis", "done", {})
    for n, outcome in enumerate(outcomes, start=1):
        run.at(n, "cutover.outcome", "verification", "running", outcome)
    return client_with(sessions, phase("verification")).get(f"/migrations/{MID}/report")


def test_report_without_cutover_outcome_has_null_field(sessions, clock):
    body = report_with_outcomes(sessions, clock).json()
    assert body["cutover_outcome"] is None
    assert body["results"]["cutover"] == []


def test_report_carries_the_cutover_outcome_in_the_web_shape(sessions, clock):
    body = report_with_outcomes(sessions, clock, OUTCOME_SWITCHED).json()
    assert body["cutover_outcome"] == OUTCOME_SWITCHED


def test_report_keeps_only_the_latest_cutover_outcome(sessions, clock):
    refused = {
        "result": "refused",
        "reason": "SWITCHOVER_FAILED",
        "message": SECRET,
        "active": "source",
        "pause_ms": None,
        "replication": {"direction": "forward", "lag_ms": None},
    }
    rolled_back = {**OUTCOME_SWITCHED, "active": "source", "held_requests": 2, "failed_requests": 0}
    body = report_with_outcomes(sessions, clock, refused, OUTCOME_SWITCHED, rolled_back).json()
    assert body["cutover_outcome"] == rolled_back


def test_report_cutover_outcome_never_copies_the_free_text_message(sessions, clock):
    # The event contract already forbids unknown keys; `message` (executor detail) is free text.
    refused = {
        "result": "refused",
        "reason": "SWITCHOVER_FAILED",
        "message": SECRET,
        "active": "source",
        "pause_ms": None,
        "steps": [{"id": "pause", "ms": 0, "window": "pause"}],
        "replication": {"direction": "forward", "lag_ms": 5},
    }
    response = report_with_outcomes(sessions, clock, refused)
    assert response.json()["cutover_outcome"] == {
        "result": "refused",
        "reason": "SWITCHOVER_FAILED",
        "active": "source",
        "pause_ms": None,
        "steps": [{"id": "pause", "ms": 0, "window": "pause"}],
        "replication": {"direction": "forward", "lag_ms": 5},
    }
    assert "hunter2" not in response.text


def test_report_reads_the_outcome_the_data_workflow_emits(sessions, clock):
    # The publisher (`cmp_data.cutover_events.run_events`) and the report agree on the keys.
    result = {"switched": True, "write_pause_ms": 7, "verify_ok": None, "tables": [],
              "steps": [{"id": "pause", "ms": 0.4, "start_ms": 0, "window": "pause"}]}  # fmt: skip
    drafts = cutover_events.run_events(
        result, attempt=1, came_from="source", going_to="target", policy=ReadinessPolicy()
    )
    outcome = next(d.data for d in drafts if d.type == "cutover.outcome")
    body = report_with_outcomes(sessions, clock, outcome).json()
    assert body["cutover_outcome"] == {
        "result": "switched",
        "active": "target",
        "pause_ms": 7,
        "steps": [{"id": "pause", "ms": 0.4, "start_ms": 0, "window": "pause"}],
        "replication": {"direction": "reverse", "lag_ms": None},
    }
