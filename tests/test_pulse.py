"""`GET /system/pulse`: CPU·메모리 파싱, 최근 5분 오류 수, 공개 엣지에 없음. /proc과 DB는 가짜로 대신한다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cmp_api import pulse
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.migration_events import MigrationEventRow, MigrationLogRow
from cmp_api.routes.migrations import get_event_sessions
from cmp_api.routes.pulse import get_report_token, get_reports
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)

STAT_A = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 1 1 1\n"
STAT_B = "cpu  200 0 200 750 150 0 0 0 0 0\n"  # 바쁨 +200, idle+iowait +100 → 66.7%
MEM = "MemTotal:       8192000 kB\nMemFree:  100 kB\nMemAvailable:   2048000 kB\n"


def iso(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pulse.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def add_event(db, status: str, seconds_ago: float, seq: int) -> None:
    db.add(
        MigrationEventRow(
            migration_id="m1",
            seq=seq,
            at=iso(seconds_ago),
            phase="traffic",
            type="traffic.snapshot",
            status=status,
            data={},
            idempotency_key=f"k{seq}",
        )
    )


def add_log(db, level: str, seconds_ago: float) -> None:
    db.add(
        MigrationLogRow(
            migration_id="m1",
            at=iso(seconds_ago),
            level=level,
            source="api",
            activity=None,
            attempt=1,
            code="X",
            fields={},
        )
    )


def test_cpu_is_the_busy_share_between_two_reads() -> None:
    assert pulse.cpu_percent(pulse.parse_cpu(STAT_A), pulse.parse_cpu(STAT_B)) == 66.7


def test_cpu_is_unknown_without_a_proc_file_or_without_time_passing() -> None:
    assert pulse.parse_cpu("") is None
    assert pulse.cpu_percent(None, pulse.parse_cpu(STAT_B)) is None
    same = pulse.parse_cpu(STAT_A)
    assert pulse.cpu_percent(same, same) is None


def test_memory_counts_available_not_free() -> None:
    assert pulse.parse_meminfo(MEM) == (8000, 6000)
    assert pulse.parse_meminfo("MemTotal: 1 kB\n") is None


def test_load_average_first_field() -> None:
    assert pulse.parse_load("0.52 0.40 0.30 1/200 999") == 0.52
    assert pulse.parse_load("") is None


def test_errors_count_failed_events_and_error_logs_in_the_last_five_minutes(sessions) -> None:
    with sessions() as db:
        add_event(db, "failed", 60, 1)  # 센다
        add_event(db, "failed", 299, 2)  # 센다
        add_event(db, "failed", 400, 3)  # 오래됨
        add_event(db, "done", 10, 4)  # 실패 아님
        add_log(db, "error", 30)  # 센다
        add_log(db, "warning", 30)  # 오류 아님
        add_log(db, "error", 900)  # 오래됨
        db.commit()
    assert pulse.count_errors(sessions, NOW) == 3


def test_errors_are_unknown_when_the_database_cannot_be_read() -> None:
    def broken():
        raise RuntimeError("db down")

    assert pulse.count_errors(broken, NOW) is None


def test_snapshot_reports_only_what_it_read(sessions) -> None:
    reads = {"stat": [STAT_A, STAT_B], "meminfo": [MEM], "loadavg": ["1.50 1 1 1/1 1"]}

    def read(name: str) -> str:
        return reads[name].pop(0) if len(reads[name]) > 1 or name == "stat" else reads[name][0]

    shot = pulse.snapshot(sessions, read=read, sleep=lambda _s: None)
    assert shot["cpu_percent"] == 66.7 and shot["mem_percent"] == 75.0
    assert shot["mem_used_mb"] == 6000 and shot["mem_total_mb"] == 8000 and shot["load1"] == 1.5
    assert shot["errors_5m"] == 0 and shot["window_seconds"] == 300


def test_snapshot_without_proc_leaves_nulls_not_made_up_numbers(sessions) -> None:
    shot = pulse.snapshot(sessions, read=lambda _n: "", sleep=lambda _s: None)
    assert shot["cpu_percent"] is None and shot["mem_percent"] is None and shot["load1"] is None


def test_the_route_returns_the_pulse(sessions) -> None:
    app.dependency_overrides[get_event_sessions] = lambda: sessions
    try:
        response = TestClient(app).get("/system/pulse")
    finally:
        app.dependency_overrides.pop(get_event_sessions, None)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "at",
        "cpu_percent",
        "mem_percent",
        "mem_used_mb",
        "mem_total_mb",
        "load1",
        "cores",
        "errors_5m",
        "window_seconds",
        "source",
        "target",
    }
    assert body["errors_5m"] == 0


def test_the_public_edge_routes_only_the_token_protected_report() -> None:
    caddy = (ROOT / "infra" / "demo" / "agent-routes.caddy").read_text(encoding="utf-8")
    assert "path /system/pulse/report" in caddy and "method POST" in caddy
    # 맥박·건강 지표 조회(GET)와 그 밖의 /system 경로는 공개하지 않는다
    paths = [line.strip() for line in caddy.splitlines() if "path" in line and "/system" in line]
    assert paths == ["path /system/pulse/report", "path /system/traffic-health/report"]


# ---- 원본·이전 서버 보고 ----


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_side_is_the_average_cpu_and_the_summed_memory_of_fresh_nodes() -> None:
    store = pulse.ReportStore(Clock())
    store.put("source", "n1", 20.0, 1000, 4000)
    store.put("source", "n2", 40.0, 3000, 4000)
    side = store.side("source")
    assert side["cpu_percent"] == 30.0 and side["mem_percent"] == 50.0
    assert side["mem_used_mb"] == 4000 and side["mem_total_mb"] == 8000 and side["nodes"] == 2
    assert store.side("target") is None  # 보고가 없으면 모름


def test_a_report_older_than_the_stale_limit_is_not_shown() -> None:
    clock = Clock()
    store = pulse.ReportStore(clock)
    store.put("target", "mac", 10.0, 100, 1000)
    clock.now += pulse.STALE_SECONDS - 1
    assert store.side("target") is not None
    clock.now += 2
    assert store.side("target") is None


def test_only_the_newest_nodes_per_role_are_kept() -> None:
    clock = Clock()
    store = pulse.ReportStore(clock)
    for i in range(pulse.MAX_NODES_PER_ROLE + 3):
        clock.now += 0.1
        store.put("source", f"n{i}", 1.0, 1, 2)
    assert store.side("source")["nodes"] == pulse.MAX_NODES_PER_ROLE


@pytest.fixture
def reporting():
    store = pulse.ReportStore()
    app.dependency_overrides[get_reports] = lambda: store
    app.dependency_overrides[get_report_token] = lambda: "secret-token"
    yield store, TestClient(app)
    app.dependency_overrides.pop(get_reports, None)
    app.dependency_overrides.pop(get_report_token, None)


GOOD = {
    "role": "source",
    "node": "eks-node-1",
    "cpu_percent": 12.5,
    "mem_used_mb": 900,
    "mem_total_mb": 3800,
}
HEADERS = {"X-Pulse-Token": "secret-token"}


def test_a_report_with_the_token_is_stored(reporting) -> None:
    store, client = reporting
    response = client.post("/system/pulse/report", json=GOOD, headers=HEADERS)
    assert response.status_code == 204 and store.side("source")["cpu_percent"] == 12.5


def test_a_report_without_or_with_a_wrong_token_is_refused(reporting) -> None:
    store, client = reporting
    assert client.post("/system/pulse/report", json=GOOD).status_code == 403
    wrong = {"X-Pulse-Token": "nope"}
    assert client.post("/system/pulse/report", json=GOOD, headers=wrong).status_code == 403
    assert store.side("source") is None


def test_reports_are_off_until_a_token_is_configured() -> None:
    app.dependency_overrides[get_report_token] = lambda: ""
    try:
        response = TestClient(app).post("/system/pulse/report", json=GOOD, headers=HEADERS)
    finally:
        app.dependency_overrides.pop(get_report_token, None)
    assert response.status_code == 503


@pytest.mark.parametrize(
    "change",
    [
        {"role": "platform"},
        {"node": "bad node!"},
        {"cpu_percent": 101},
        {"mem_total_mb": 0},
        {"mem_used_mb": 9999},  # 전체보다 큼
        {"extra": 1},
    ],
)
def test_a_malformed_report_is_refused(reporting, change) -> None:
    store, client = reporting
    response = client.post("/system/pulse/report", json={**GOOD, **change}, headers=HEADERS)
    assert response.status_code == 422 and store.side("source") is None


def test_the_pulse_includes_what_the_servers_reported(reporting, sessions) -> None:
    store, client = reporting
    store.put("source", "n1", 25.0, 1000, 4000)
    store.put("target", "mac", 10.0, 2000, 8000)
    app.dependency_overrides[get_event_sessions] = lambda: sessions
    try:
        body = client.get("/system/pulse").json()
    finally:
        app.dependency_overrides.pop(get_event_sessions, None)
    assert body["source"]["cpu_percent"] == 25.0 and body["target"]["mem_percent"] == 25.0
