"""Ticket 24: SSE snapshot, Last-Event-ID, heartbeat, termination, legacy status, JSON pages."""

import json

import pytest
from cmp_api import event_stream
from cmp_api.db import Base
from cmp_api.event_stream import StatusUnavailable, migration_status
from cmp_api.main import app
from cmp_api.migration_events import DbEventEmitter
from cmp_api.routes.migrations import get_event_sessions, get_status_reader
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

MID = "migration-sse"
CHECK = {"name": "pods_ready", "state": "done"}


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sse.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(event_stream, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(event_stream, "STATUS_EVERY_SECONDS", 0.0)


def client_with(sessions, states):
    """`states`: Workflow query results in order (last repeats); an Exception is raised."""
    calls = {"n": 0}

    async def read(_):
        value = states[min(calls["n"], len(states) - 1)]
        calls["n"] += 1
        if isinstance(value, Exception):
            raise value
        return value

    app.dependency_overrides[get_event_sessions] = lambda: sessions
    app.dependency_overrides[get_status_reader] = lambda: read
    return TestClient(app), calls


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def emit(sessions, n, migration_id=MID):
    emitter = DbEventEmitter(sessions)
    for i in range(n):
        emitter.emit(
            migration_id,
            "provision.check",
            "provisioning",
            "running",
            CHECK,
            idempotency_key=f"k{i}",
        )


def blocks(text: str) -> list[dict]:
    out = []
    for raw in text.strip().split("\n\n"):
        block = {"comment": raw.startswith(":")}
        for line in raw.split("\n"):
            field, _, value = line.partition(": ")
            if field in ("id", "event", "data"):
                block[field] = json.loads(value) if field == "data" else value
        out.append(block)
    return out


def phase(name, **extra):
    return {"phase": name, "approval_pending": False, "cancel_requested": False, **extra}


def test_new_connection_gets_reset_snapshot_with_all_events_then_closes_on_done(sessions):
    emit(sessions, 3)
    client, _ = client_with(sessions, [phase("scaffold_complete")])
    response = client.get(f"/migrations/{MID}/events?legacy=false")
    assert response.headers["content-type"].startswith("text/event-stream")
    [snap] = blocks(response.text)
    assert snap["id"] == "3"
    data = snap["data"]
    assert data["type"] == "snapshot" and data["seq"] == 3 and data["status"] == "done"
    assert data["data"]["reset"] is True and data["data"]["status"] == "done"
    assert [e["seq"] for e in data["data"]["events"]] == [1, 2, 3]
    assert all("migration_id" not in e for e in data["data"]["events"])


def test_reconnect_sends_only_events_after_last_event_id_with_id_lines(sessions):
    emit(sessions, 4)
    client, _ = client_with(sessions, [phase("cancelled")])
    response = client.get(f"/migrations/{MID}/events?legacy=false", headers={"Last-Event-ID": "2"})
    snap, *rest = blocks(response.text)
    assert snap["data"]["data"] == {
        "migration_id": MID,
        "status": "cancelled",
        "reset": False,
        "events": [],
    }
    assert snap["id"] == "2"
    assert [(b["id"], b["data"]["seq"]) for b in rest] == [("3", 3), ("4", 4)]


@pytest.mark.parametrize(
    ("workflow_phase", "status"),
    [
        ("failed", "failed"),
        ("plan_failed", "failed"),
        ("provisioning_failed", "failed"),
        ("approval_expired", "failed"),
        ("cancelled", "cancelled"),
        ("scaffold_complete", "done"),
    ],
)
def test_terminal_states_close_the_stream(sessions, workflow_phase, status):
    emit(sessions, 1)
    client, _ = client_with(sessions, [phase("provisioning"), phase(workflow_phase)])
    response = client.get(f"/migrations/{MID}/events?legacy=false")  # returns => stream closed
    assert blocks(response.text)[0]["data"]["data"]["status"] == "running"
    assert migration_status(phase(workflow_phase), None) == status


def test_live_events_stream_and_are_drained_before_close(sessions):
    def later():
        emit(sessions, 2)  # stored while the stream is open
        return phase("scaffold_complete")

    states = [phase("provisioning"), phase("provisioning")]
    client, calls = client_with(sessions, states)

    async def read(_):
        calls["n"] += 1
        return phase("provisioning") if calls["n"] < 3 else later()

    app.dependency_overrides[get_status_reader] = lambda: read
    response = client.get(f"/migrations/{MID}/events?legacy=false")
    snap, *rest = blocks(response.text)
    assert snap["data"]["data"]["events"] == []
    assert [b["data"]["seq"] for b in rest] == [1, 2]


def test_heartbeat_comment_when_idle(sessions, monkeypatch):
    monkeypatch.setattr(event_stream, "HEARTBEAT_SECONDS", 0.0)
    emit(sessions, 1)
    client, _ = client_with(sessions, [phase("provisioning")] * 4 + [phase("scaffold_complete")])
    response = client.get(f"/migrations/{MID}/events?legacy=false")
    assert ": heartbeat" in response.text


def test_legacy_status_blocks_are_kept_by_default(sessions):
    emit(sessions, 1)
    client, _ = client_with(sessions, [phase("planning"), phase("scaffold_complete")])
    legacy = [b for b in blocks(client.get(f"/migrations/{MID}/events").text) if b.get("event")]
    assert [b["data"]["phase"] for b in legacy] == ["planning", "scaffold_complete"]
    assert all("id" not in b for b in legacy)


def test_without_workflow_status_comes_from_events(sessions):
    DbEventEmitter(sessions).emit(
        "poc-1",
        "error",
        "verification",
        "failed",
        {"code": "SWITCHOVER_FAILED", "message": "", "phase": "verification", "retryable": False},
    )
    client, _ = client_with(sessions, [StatusUnavailable()])
    snap = blocks(client.get("/migrations/poc-1/events").text)[0]
    assert snap["data"]["data"]["status"] == "failed"


def test_unknown_migration_is_404(sessions):
    client, _ = client_with(sessions, [None])
    assert client.get("/migrations/none/events").status_code == 404


def test_json_page_after_seq(sessions):
    emit(sessions, 3)
    client, _ = client_with(sessions, [phase("provisioning")])
    page = client.get(f"/migrations/{MID}/events?after=1&limit=1").json()
    assert page["migration_id"] == MID and page["next_after"] == 2
    [event] = page["events"]
    assert event["seq"] == 2 and event["migration_id"] == MID and event["data"] == CHECK
    accept = client.get(f"/migrations/{MID}/events", headers={"Accept": "application/json"})
    assert [e["seq"] for e in accept.json()["events"]] == [1, 2, 3]


def test_logs_page(sessions):
    emitter = DbEventEmitter(sessions)
    emitter.log(MID, "warning", "TERRAFORM_RESOURCE_FAILED", {"address": "aws_eks_cluster.this"})
    client, _ = client_with(sessions, [phase("provisioning")])
    page = client.get(f"/migrations/{MID}/logs").json()
    assert page["next_after"] == 1
    assert page["logs"][0]["code"] == "TERRAFORM_RESOURCE_FAILED"


def test_unknown_last_event_id_falls_back_to_a_reset_snapshot(sessions):
    emit(sessions, 2)
    client, _ = client_with(sessions, [phase("scaffold_complete")])
    response = client.get(f"/migrations/{MID}/events?legacy=false", headers={"Last-Event-ID": "99"})
    snap = blocks(response.text)[0]["data"]
    assert snap["data"]["reset"] is True and [e["seq"] for e in snap["data"]["events"]] == [1, 2]
