"""데이터 이전 Activity(ticket 02·03) 시험. DB Pod는 가짜로 바꾼다(실제 DB 시험은 test_data_lab.py).

시험 경계: Activity 결과·발행된 이벤트·가짜 DB에 남은 상태. SQL 문법과 카탈로그 값이 실제
PostgreSQL에서 맞는지는 잡지 못한다.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.events import validate_data
from cmp_data import sql as q
from cmp_worker.activities import data_migration as dm
from cmp_worker.events import configure_emitter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

MID = "migration-3f2b9c1e-5d4a-4c8b-9e7f-0a1b2c3d4e5f"


def endpoint(ref: str, host: str) -> dict:
    return {
        "access": "KUBECONFIG",
        "kubeconfig_ref": ref,
        "namespace": "data-lab",
        "pod": "db-0",
        "peer_host": host,
        "dbname": "chat",
    }


REQUEST = {
    "migration_id": MID,
    "source": endpoint("src", "10.0.0.1"),
    "target": endpoint("dst", "10.0.0.2"),
}


class FakeDb:
    def __init__(self, *, tables=(), wal="logical", facts=None):
        self.tables = set(tables)
        self.wal = wal
        self.facts = facts or {}
        self.publications: set[str] = set()
        self.subscriptions: set[str] = set()
        self.secrets: dict[str, str] = {}
        self.executed: list[str] = []
        self.restarts = 0
        self.pending_wal: str | None = None
        self.sync = {"tables": [], "target_lsn": None}
        self.source = {"source_lsn": "0/1", "lag_bytes": None, "rows_total": {}}

    def query(self, sql: str) -> str:
        if sql == q.PRECHECK_SQL:
            base = {
                "server_version_num": 160004,
                "wal_level": self.wal,
                "max_replication_slots": 10,
                "slots_in_use": 0,
                "tables_without_identity": [],
                "sequences": [],
                "large_objects": 0,
                "unlogged_tables": 0,
                "extensions": [],
            }
            return json.dumps(base | self.facts)
        if sql == q.WAL_LEVEL_SQL:
            return self.wal
        if sql == q.USER_TABLES_SQL:
            return json.dumps(sorted(self.tables))
        if "from pg_publication" in sql:
            return str(sum(1 for p in self.publications if p in sql))
        if "from pg_subscription where" in sql:
            return str(sum(1 for s in self.subscriptions if s in sql))
        if "pg_subscription_rel" in sql:
            return json.dumps(self.sync)
        if "pg_current_wal_lsn()::text" in sql:
            return json.dumps(self.source)
        raise AssertionError(f"unexpected query: {sql[:60]}")

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if sql == q.SET_WAL_LOGICAL_SQL:
            self.pending_wal = "logical"
        elif sql.startswith("create publication"):
            self.publications.add(sql.split()[2])
        elif sql.startswith("create subscription"):
            self.subscriptions.add(sql.split()[2])

    def dump_schema(self) -> bytes:
        return b"-- schema " + ",".join(sorted(self.tables)).encode()

    def restore(self, script: bytes) -> None:
        self.tables |= set(script.decode().removeprefix("-- schema ").split(","))

    def restart(self, timeout: int = 180) -> None:
        self.restarts += 1
        self.wal = self.pending_wal or self.wal

    def read_secret(self, name: str) -> str | None:
        return self.secrets.get(name)

    def create_secret(self, name: str, value: str) -> None:
        assert name not in self.secrets
        self.secrets[name] = value


@pytest.fixture
def dbs(monkeypatch):
    pods = {
        "src": FakeDb(tables={"public.messages", "public.rooms"}),
        "dst": FakeDb(wal="replica"),
    }

    @contextmanager
    def fake_open(ep):
        yield pods[ep.kubeconfig_ref]

    monkeypatch.setattr(dm, "OPEN_DB", fake_open)
    return pods


@pytest.fixture
def emitter():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


async def run(activity, payload):
    return await ActivityEnvironment().run(activity, payload)


def emitted(emitter) -> list[tuple[str, dict]]:
    return [(e.type, e.data) for e in emitter.events]


async def test_precheck_blocks_and_emits_blocked_phase(dbs, emitter) -> None:
    dbs["src"].wal = "replica"
    result = await run(dm.dm_precheck, REQUEST)
    assert not result["ok"] and result["blockers"] == ["WAL_LEVEL_NOT_LOGICAL"]
    assert emitted(emitter) == [
        ("data.phase", {"phase": "preflight"}),
        ("data.phase", {"phase": "blocked"}),
    ]


async def test_prepare_turns_on_logical_wal_on_the_subscriber_and_restarts_once(dbs) -> None:
    actions = [["REPLICA_IDENTITY_FULL", "public.audit_log"]]
    first = await run(dm.dm_prepare, {"request": REQUEST, "actions": actions})
    again = await run(dm.dm_prepare, {"request": REQUEST, "actions": actions})
    assert first["subscriber_restarted"] and not again["subscriber_restarted"]
    assert dbs["dst"].restarts == 1
    assert "alter table public.audit_log replica identity full" in dbs["src"].executed[0]


async def test_copy_schema_copies_once_and_stops_on_a_partial_target(dbs, emitter) -> None:
    assert (await run(dm.dm_copy_schema, REQUEST))["copied"] is True
    assert (await run(dm.dm_copy_schema, REQUEST))["copied"] is False
    dbs["dst"].tables = {"public.messages"}
    with pytest.raises(ApplicationError) as err:
        await run(dm.dm_copy_schema, REQUEST)
    assert err.value.type == "TARGET_SCHEMA_PARTIAL"


async def test_subscribe_is_idempotent_and_keeps_the_password_out_of_results(dbs) -> None:
    first = await run(dm.dm_subscribe, REQUEST)
    again = await run(dm.dm_subscribe, REQUEST)
    assert first["created_publication"] and first["created_subscription"]
    assert not again["created_publication"] and not again["created_subscription"]
    [password] = dbs["dst"].secrets.values()
    assert password not in json.dumps([first, again])
    create = next(s for s in dbs["dst"].executed if s.startswith("create subscription"))
    assert "host=''10.0.0.1''" in create  # 구독자는 발행자의 peer_host(터널 주소)로 접속한다


async def test_reverse_direction_swaps_publisher_and_subscriber(dbs) -> None:
    await run(dm.dm_subscribe, REQUEST | {"direction": "reverse"})
    assert any("_rev_pub" in p for p in dbs["dst"].publications)
    assert any(s.endswith("_rev") for s in dbs["src"].subscriptions)


async def test_sample_emits_web_valid_data_events(dbs, emitter) -> None:
    dbs["dst"].sync = {
        "tables": [{"name": "public.messages", "state": "d", "rows_copied": 120}],
        "target_lsn": "0/10",
    }
    dbs["src"].source = {
        "source_lsn": "0/20",
        "lag_bytes": 16,
        "rows_total": {"public.messages": 500},
    }
    result = await run(dm.dm_sample, REQUEST)
    assert result["phase"] == "copy"
    events = emitted(emitter)
    assert [t for t, _ in events] == ["data.phase", "data.lsn", "data.table", "data.progress"]
    for event_type, data in events:
        validate_data(event_type, data)
    assert events[2][1] == {
        "name": "public.messages",
        "rows_copied": 120,
        "rows_total": 500,
        "state": "copying",
    }


def _sync(dbs, tables, totals, *, lag=None) -> None:
    dbs["dst"].sync = {"tables": tables, "target_lsn": "0/10"}
    dbs["src"].source = {"source_lsn": "0/20", "lag_bytes": lag, "rows_total": totals}


def progress_events(emitter) -> list:
    return [e for e in emitter.events if e.type == "data.progress"]


async def test_sample_reports_copy_progress_from_the_same_sample(dbs, emitter) -> None:
    """복제 감시 한 번 = data.progress 한 번. 행 수는 data.table과 같은 값(새 조회 없음)."""
    _sync(
        dbs,
        [
            {"name": "public.messages", "state": "d", "rows_copied": 300},
            {"name": "public.rooms", "state": "r", "rows_copied": 100},
        ],
        {"public.messages": 900, "public.rooms": 100},
        lag=4096,
    )
    await run(dm.dm_sample, REQUEST)
    [event] = progress_events(emitter)
    assert event.phase == "data_copy" and event.status == "running"
    assert event.data == {
        "step": "dump",
        "rows": 400,
        "total_rows": 1000,
        "lag_ms": None,
        "writes_per_sec": 0.0,
        "dump_percent": 40.0,
    }
    validate_data("data.progress", event.data)
    assert emitted(emitter)[-1][0] == "data.progress"  # 웹 틱처럼 lsn·table 뒤에 온다


@pytest.mark.parametrize(
    ("states", "lag", "step", "status", "percent"),
    [
        ([], None, "dump", "running", 0.0),  # 구독했지만 아직 테이블 없음(schema)
        (["r", "r"], None, "cdc", "running", 100.0),  # 복사 끝, 지연 모름(catchup)
        (["r", "r"], 10, "ready", "done", 100.0),  # streaming
    ],
)
async def test_progress_step_follows_the_replication_phase(
    dbs, emitter, states, lag, step, status, percent
) -> None:
    tables = [
        {"name": f"public.t{i}", "state": s, "rows_copied": 50} for i, s in enumerate(states)
    ]
    _sync(dbs, tables, {f"public.t{i}": 50 for i in range(len(states))}, lag=lag)
    await run(dm.dm_sample, REQUEST)
    [event] = progress_events(emitter)
    assert (event.data["step"], event.status, event.data["dump_percent"]) == (step, status, percent)
    validate_data("data.progress", event.data)


async def test_progress_percent_stays_in_range_with_estimated_totals(dbs, emitter) -> None:
    # rows_total은 원본 n_live_tup(근사치)이라 복사한 행이 더 클 수 있다.
    _sync(dbs, [{"name": "public.m", "state": "d", "rows_copied": 130}], {"public.m": 100})
    await run(dm.dm_sample, REQUEST)
    [event] = progress_events(emitter)
    assert event.data["dump_percent"] == 100.0
    _sync(dbs, [{"name": "public.m", "state": "d", "rows_copied": 0}], {})
    await run(dm.dm_sample, REQUEST)
    assert progress_events(emitter)[-1].data["dump_percent"] == 0.0


async def test_progress_is_not_duplicated_on_re_emit_and_has_no_secrets(dbs, emitter) -> None:
    await run(dm.dm_subscribe, REQUEST)
    _sync(dbs, [{"name": "public.m", "state": "d", "rows_copied": 5}], {"public.m": 10})
    env = ActivityEnvironment()
    await env.run(dm.dm_sample, REQUEST)
    await env.run(dm.dm_sample, REQUEST)  # 같은 Activity 시도의 재발행은 한 줄로 남는다
    assert len(progress_events(emitter)) == 1
    [password] = dbs["dst"].secrets.values()
    dumped = json.dumps([e.data for e in emitter.events])
    assert password not in dumped and "password" not in dumped and "10.0.0.1" not in dumped


async def test_missing_kubeconfig_is_a_non_retryable_configuration_error(monkeypatch) -> None:
    monkeypatch.delenv("DATA_MIGRATION_KUBECONFIG_DIR", raising=False)
    with pytest.raises(ApplicationError) as err:
        await run(dm.dm_subscribe, REQUEST)
    assert err.value.type == "DbAccessNotConfigured" and err.value.non_retryable
