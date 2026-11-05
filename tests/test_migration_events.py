"""Ticket 24: emitters — names, checks, masking, size limit, seq, idempotency, Activity-only."""

import threading

import pytest
from cmp_api.db import Base
from cmp_api.migration_events import (
    Actor,
    DbEventEmitter,
    InMemoryEmitter,
    MigrationEventRow,
    append_event,
    list_events,
    list_logs,
)
from cmp_worker.events import (
    configure_emitter,
    emit_event,
    emit_migration_event,
    log_event,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

MID = "migration-1"
CHECK = {"name": "pods_ready", "state": "done"}


@pytest.fixture
def memory():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'events.db'}", connect_args={"timeout": 30}
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_in_memory_emit_translates_validates_and_numbers(memory):
    first = emit_event(MID, "provision.check", "provisioning", "running", CHECK)
    second = emit_event(
        MID,
        "data.readiness",
        "data_copy",
        "running",
        {"longest_xact_ms": 5, "lock_waiters": 0, "commits_per_sec": 2.0, "ready": True},
        progress=40,
    )
    other = emit_event("migration-2", "provision.check", "provisioning", "running", CHECK)
    assert (first.seq, second.seq, other.seq) == (1, 2, 1)
    assert second.data == {
        "longest_tx_ms": 5,
        "lock_waits": 0,
        "commits_per_sec": 2.0,
        "ready": True,
    }
    payload = second.sse_payload()
    assert "migration_id" not in payload and payload["progress"] == 40
    assert payload["at"].endswith("Z")


@pytest.mark.parametrize(
    "args",
    [
        ("provision.check", "provisioning", "running", {"name": "x"}),  # missing state
        ("provision.check", "deploying", "running", CHECK),  # unknown phase
        ("provision.check", "provisioning", "ok", CHECK),  # unknown status
        ("no.such", "provisioning", "running", {}),
        ("snapshot", "provisioning", "running", {}),
    ],
)
def test_invalid_events_are_dropped_with_a_log_not_raised(memory, args):
    assert emit_event(MID, *args) is None
    assert memory.events == []
    assert memory.logs[-1]["code"] == "EVENT_INVALID"


def test_data_over_the_size_limit_is_rejected(memory):
    huge = {"id": "f", "check": "c", "lines": [], "why": "x" * 40_000, "evidence": ""}
    assert emit_event(MID, "build.fix", "build", "running", huge) is None
    assert memory.logs[-1]["code"] == "EVENT_TOO_LARGE"


def test_secret_values_are_masked_but_references_and_ids_kept(memory):
    event = emit_event(
        MID,
        "build.fix",
        "build",
        "running",
        {
            "id": "fix-1",
            "check": "secret",
            "lines": [{"kind": "add", "text": "AWS_KEY=AKIAABCDEFGHIJKLMNOP"}],
            "why": "token: ghp_abcdefghijklmnopqrstuvwxyz0123",
            "evidence": "postgresql://app:hunter22@db:5432/app",
        },
    )
    text = str(event.data)
    assert "AKIAABCDEFGHIJKLMNOP" not in text and "hunter22" not in text
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in text
    log = log_event(
        MID,
        "error",
        "HELM_FAILED",
        {"password": "s3cr3t-value", "db_secret_ref": "db-auth/password"},
    )
    assert log["fields"] == {"password": "[MASKED]", "db_secret_ref": "db-auth/password"}


def test_same_key_in_one_attempt_is_stored_once(memory):
    a = emit_event(MID, "provision.check", "provisioning", "running", CHECK)
    b = emit_event(MID, "provision.check", "provisioning", "running", CHECK)
    c = emit_event(
        MID, "provision.check", "provisioning", "running", CHECK, idempotency_key="again"
    )
    assert a.seq == b.seq == 1 and c.seq == 2


async def test_activity_retry_attempt_is_a_new_event(memory):
    async def run(attempt: int):
        env = ActivityEnvironment()
        env.info = env.info.__class__(**{**env.info.__dict__, "attempt": attempt})
        return await env.run(
            emit_migration_event,
            {
                "migration_id": MID,
                "type": "provision.check",
                "phase": "provisioning",
                "status": "running",
                "data": CHECK,
            },
        )

    assert await run(1) == 1
    assert await run(1) == 1  # same attempt, same data → deduplicated
    assert await run(2) == 2  # Temporal retry → new progress


def test_emit_in_workflow_code_is_refused(memory, monkeypatch):
    from temporalio import workflow

    monkeypatch.setattr(workflow, "in_workflow", lambda: True)
    with pytest.raises(RuntimeError):
        emit_event(MID, "provision.check", "provisioning", "running", CHECK)


def test_db_seq_is_monotonic_per_migration_and_idempotent(db):
    emitter = DbEventEmitter(db)
    seqs = [
        emitter.emit(
            MID, "provision.check", "provisioning", "running", CHECK, idempotency_key=str(i)
        ).seq
        for i in range(3)
    ]
    again = emitter.emit(
        MID, "provision.check", "provisioning", "running", CHECK, idempotency_key="1"
    )
    other = emitter.emit("migration-2", "provision.check", "provisioning", "running", CHECK)
    assert seqs == [1, 2, 3] and again.seq == 2 and other.seq == 1
    with db() as session:
        assert [e.seq for e in list_events(session, MID, after=1)] == [2, 3]
        assert session.scalars(select(MigrationEventRow)).all().__len__() == 4


def test_db_concurrent_writers_get_distinct_contiguous_seq(db):
    emitter = DbEventEmitter(db)
    results: list[int] = []

    def worker(n: int):
        for i in range(5):
            event = emitter.emit(
                MID,
                "provision.check",
                "provisioning",
                "running",
                CHECK,
                idempotency_key=f"{n}-{i}",
                actor=Actor(source="worker", workflow_id=f"w{n}"),
            )
            results.append(event.seq)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == list(range(1, 21))


def test_db_logs_are_listed_after_an_id(db):
    emitter = DbEventEmitter(db)
    emitter.log(MID, "info", "TERRAFORM_APPLY_STARTED", {"plan_id": "p"})
    emitter.log(MID, "error", "TERRAFORM_APPLY_FAILED", {"attempt": 1})
    with db() as session:
        logs = list_logs(session, MID, after=1)
    assert [log["code"] for log in logs] == ["TERRAFORM_APPLY_FAILED"]


def test_append_event_raises_for_invalid_input(db):
    from cmp_api.migration_events import EventRejected

    with db() as session, pytest.raises(EventRejected):
        append_event(session, MID, "provision.check", "provisioning", "running", {})
