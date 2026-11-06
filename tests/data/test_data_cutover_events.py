"""DB 전환 계획·결과 → 웹 이벤트(cmp_data.cutover_events), 실행기 단계 기록, 두 번째 승인 API."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from cmp_api.main import app
from cmp_api.routes import migrations as routes
from cmp_contracts.events import validate_data
from cmp_data import ReadinessPolicy
from cmp_data import cutover_events as ce
from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

POLICY = ReadinessPolicy()
RUNNER = Path(__file__).resolve().parents[2] / "infra/data-migration/cutover/switchover.py"


def runner_module():
    spec = importlib.util.spec_from_file_location("cmp_switchover_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(drafts) -> None:
    for draft in drafts:
        validate_data(draft.type, draft.data)


# ---- 실행기 결과 → 이벤트 ----


def test_rollback_result_reports_source_active_and_forward_replication() -> None:
    result = {"switched": True, "write_pause_ms": 5.5, "verify_ok": True,
              "steps": [{"id": "pause", "ms": 0.4, "start_ms": 0, "window": "pause"}],
              "tables": [], "ready": None}  # fmt: skip
    drafts = ce.run_events(result, attempt=2, came_from="target", going_to="source", policy=POLICY)
    check(drafts)
    outcome = next(d for d in drafts if d.type == "cutover.outcome")
    assert outcome.data["active"] == "source"
    assert outcome.data["replication"]["direction"] == "forward"  # 다시 원본 → 대상
    assert len({d.key for d in drafts}) == len(drafts)  # 이벤트마다 멱등 키가 다르다


def test_mismatch_marks_the_table_and_fails_verification() -> None:
    tables = [
        {"table": "messages", "source_rows": 10, "target_rows": 7, "match": False},
        {"table": "rooms", "source_rows": 2, "target_rows": 2, "match": True},
    ]
    result = {"switched": True, "write_pause_ms": 9, "verify_ok": False, "steps": [],
              "tables": tables}  # fmt: skip
    drafts = ce.run_events(result, attempt=1, came_from="source", going_to="target", policy=POLICY)
    check(drafts)
    verify = {d.data["table"]: d.data["checksum"] for d in drafts if d.type == "cutover.verify"}
    assert verify == {"messages": "mismatch", "rooms": "match"}
    totals = {d.data["name"]: d.data for d in drafts if d.type == "data.verify"}
    assert totals["rows"] == {"name": "rows", "state": "failed", "source": 12, "target": 9}
    assert totals["checksum"]["state"] == "failed"


def test_verify_totals_follow_the_web_order() -> None:
    """웹 script.ts DATA_VERIFY_TIMELINE: rows running → rows done → checksum running → checksum done."""
    tables = [
        {"table": "messages", "source_rows": 10, "target_rows": 10, "match": True},
        {"table": "rooms", "source_rows": 2, "target_rows": 2, "match": True},
    ]
    result = {"switched": True, "write_pause_ms": 9, "verify_ok": True, "steps": [],
              "tables": tables}  # fmt: skip
    drafts = ce.run_events(result, attempt=1, came_from="source", going_to="target", policy=POLICY)
    check(drafts)
    verify = [(d.status, d.data) for d in drafts if d.type == "data.verify"]
    assert verify == [
        ("running", {"name": "rows", "state": "running"}),
        ("running", {"name": "rows", "state": "done", "source": 12, "target": 12}),
        ("running", {"name": "checksum", "state": "running"}),
        ("done", {"name": "checksum", "state": "done"}),
    ]
    # 테이블별 결과(cutover.verify) 뒤에 합계가 온다
    types = [d.type for d in drafts]
    assert types.index("data.verify") > max(i for i, t in enumerate(types) if t == "cutover.verify")
    assert len({d.key for d in drafts}) == len(drafts)


def test_failed_verify_totals_end_failed() -> None:
    tables = [{"table": "messages", "source_rows": 10, "target_rows": 7, "match": False}]
    result = {"switched": True, "write_pause_ms": 9, "verify_ok": False, "steps": [],
              "tables": tables}  # fmt: skip
    drafts = ce.run_events(result, attempt=1, came_from="source", going_to="target", policy=POLICY)
    verify = [
        (d.status, d.data["name"], d.data["state"]) for d in drafts if d.type == "data.verify"
    ]
    assert verify == [
        ("running", "rows", "running"),
        ("failed", "rows", "failed"),
        ("running", "checksum", "running"),
        ("failed", "checksum", "failed"),
    ]


def test_internal_marker_table_is_not_shown_as_a_user_table() -> None:
    tables = [
        {"table": "messages", "source_rows": 5, "target_rows": 5, "match": True},
        {"table": "cmp_switchover_marker", "source_rows": 1, "target_rows": 1, "match": True},
    ]
    result = {"switched": True, "write_pause_ms": 6, "verify_ok": True, "steps": [],
              "tables": tables}  # fmt: skip
    drafts = ce.run_events(result, attempt=1, came_from="source", going_to="target", policy=POLICY)
    check(drafts)
    assert [d.data["table"] for d in drafts if d.type == "cutover.verify"] == ["messages"]
    rows = [d.data for d in drafts if d.type == "data.verify" and d.data["name"] == "rows"][-1]
    assert (rows["source"], rows["target"]) == (5, 5)


def test_verify_none_sends_no_table_results() -> None:
    result = {"switched": True, "write_pause_ms": 4, "verify_ok": None, "steps": []}
    drafts = ce.run_events(result, attempt=1, came_from="source", going_to="target", policy=POLICY)
    assert not [d for d in drafts if d.type in ("cutover.verify", "data.verify")]


def test_refusal_before_pause_has_no_revert_step() -> None:
    result = {"switched": False, "code": "DIRECT_CLIENTS", "detail": "2 sessions bypass PgBouncer",
              "paused": False, "steps": [], "ready": None}  # fmt: skip
    drafts = ce.run_events(result, attempt=1, came_from="source", going_to="target", policy=POLICY)
    check(drafts)
    assert [d.type for d in drafts] == ["cutover.ready", "cutover.outcome"]
    assert drafts[-1].status == "waiting" and drafts[-1].data["reason"] == "DIRECT_CLIENTS"


def test_not_ready_reports_what_the_runner_waited_on() -> None:
    ready = {"longest_xact_ms": 900, "lock_waiters": 1, "lag_bytes": 0, "ready": False,
             "reasons": ["LONG_TRANSACTION", "LOCK_WAIT"], "waited_ms": 30000.0}  # fmt: skip
    data = ce.ready_data(1, ready, POLICY, commits_per_sec=12.5)
    validate_data("cutover.ready", data)
    assert data["waiting_on"] == "LONG_TRANSACTION,LOCK_WAIT" and not data["ready"]
    assert data["limits"] == {"longest_tx_ms": POLICY.max_xact_ms, "lock_waits": 0}


@pytest.mark.parametrize(
    ("sample", "ready"),
    [
        ({"phase": "streaming", "lag_bytes": 0}, True),
        ({"phase": "streaming", "lag_bytes": 0, "stale": True}, False),  # 감시 실패 값
        ({"phase": "catchup", "lag_bytes": 10}, False),
        ({"phase": "streaming", "lag_bytes": None}, False),
    ],
)
def test_plan_waits_until_replication_is_streaming(sample, ready) -> None:
    assert ce.plan_ready(sample, POLICY) is ready


# ---- 실행기 단계 기록 ----


def test_runner_timeline_measures_pause_steps_from_pause_start() -> None:
    runner = runner_module()
    ts = {"prepare": 0, "seq_prebump": 1, "sync_on": 2.5, "wait_ready": 3, "start": 10,
          "paused": 10.5, "marker": 12, "snapshot": 12.5, "subs": 13, "live": 13.2,
          "retargeted": 13.9, "resumed": 14}  # fmt: skip
    steps = runner.timeline(ts)
    by_id = {s["id"]: s for s in steps}
    assert [s["id"] for s in steps] == [i for i, _ in ce.PLAN_STEPS]
    assert by_id["seq_raise"] == {"id": "seq_raise", "ms": 1.5, "window": "pre"}
    assert by_id["marker"] == {"id": "marker", "ms": 1.5, "start_ms": 0.5, "window": "pause"}
    assert by_id["sub_off"]["start_ms"] == by_id["reverse"]["start_ms"] == 2.5  # 병렬
    # 실패한 지점까지만 남는다
    partial = runner.timeline({k: ts[k] for k in ("prepare", "seq_prebump", "sync_on",
                                                  "wait_ready", "start", "paused")})  # fmt: skip
    assert [s["id"] for s in partial][-1] == "pause"


def test_runner_table_rows_compares_each_table() -> None:
    runner = runner_module()
    rows = runner.table_rows({"a": ["3", "x"], "b": ["1", "y"]}, {"a": ["3", "x"]})
    assert rows == [
        {"table": "a", "source_rows": 3, "target_rows": 3, "match": True},
        {"table": "b", "source_rows": 1, "target_rows": None, "match": False},
    ]


# ---- 두 번째 승인 API ----


class FakeHandle:
    def __init__(self, status: dict | None):
        self.status, self.signals = status, []

    async def query(self, name: str):
        assert name == "status"
        if self.status is None:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return self.status

    async def signal(self, name: str) -> None:
        self.signals.append(name)


@pytest.fixture
def handles(monkeypatch):
    table: dict[str, FakeHandle] = {}

    async def handle(workflow_id: str):
        return table.setdefault(workflow_id, FakeHandle(None))

    monkeypatch.setattr(routes, "_handle", handle)
    return table


def test_cutover_approval_signals_the_data_migration_child(handles) -> None:
    child = handles["data-migration-1"] = FakeHandle({"phase": "streaming", "active": "source"})
    response = TestClient(app).post("/migrations/migration-1/approve?gate=cutover")
    assert response.status_code == 202
    assert response.json() == {"workflow_id": "data-migration-1", "signal": "switchover"}
    assert child.signals == ["switchover"]


@pytest.mark.parametrize(
    "status",
    [{"phase": "copy", "active": "source"}, {"phase": "switched", "active": "target"},
     {"phase": "switching", "active": "source"}],
)  # fmt: skip
def test_cutover_approval_is_refused_until_replication_is_ready(handles, status) -> None:
    child = handles["data-migration-1"] = FakeHandle(status)
    response = TestClient(app).post("/migrations/migration-1/approve?gate=cutover")
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "CUTOVER_NOT_READY", "phase": status["phase"]}
    assert child.signals == []


def test_cutover_approval_without_data_migration_is_not_found(handles) -> None:
    response = TestClient(app).post("/migrations/migration-1/approve?gate=cutover")
    assert response.status_code == 404


def test_plain_approval_still_signals_the_migration(handles) -> None:
    parent = handles["migration-1"] = FakeHandle({})
    response = TestClient(app).post("/migrations/migration-1/approve")
    assert response.status_code == 202 and parent.signals == ["approve"]
    assert TestClient(app).post("/migrations/migration-1/approve?gate=other").status_code == 422
