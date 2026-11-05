"""데이터 이전 SQL 결과 해석·인용 단위 시험."""

from __future__ import annotations

import json

from cmp_data import replication_names
from cmp_data import sql as q


def test_conninfo_escapes_quotes_and_backslashes() -> None:
    info = q.conninfo(host="10.0.0.1", password="a'b\\c d")
    assert info == "host='10.0.0.1' password='a\\'b\\\\c d'"


def test_subscription_sql_wraps_the_connection_string_as_one_literal() -> None:
    names = replication_names("migration-abc", "forward")
    sql = q.create_subscription_sql(
        names, host="h", port=5432, dbname="chat", password="p'w", copy_data=True
    )
    assert sql.startswith(f"create subscription {names.subscription} connection '")
    # conninfo가 p\'w로 이스케이프하고, SQL 상수가 작은따옴표를 한 번 더 겹친다.
    assert "password=''p\\''w''" in sql
    assert f"slot_name = '{names.slot}'" in sql


def test_parse_sync_joins_target_tables_with_source_totals() -> None:
    target = {
        "tables": [{"name": "public.messages", "state": "r", "rows_copied": 20000}],
        "target_lsn": "0/24A65D0",
    }
    source = {"source_lsn": "0/24A65D0", "lag_bytes": 0, "rows_total": {"public.messages": 20000}}
    sample = q.parse_sync(json.dumps(target), json.dumps(source))
    assert sample.lag_bytes == 0
    assert sample.tables[0].rows_total == 20000
    assert sample.target_lsn == "0/24A65D0"


def test_parse_sync_keeps_a_missing_slot_as_unknown_lag() -> None:
    sample = q.parse_sync(
        json.dumps({"tables": [], "target_lsn": None}),
        json.dumps({"source_lsn": "0/1", "lag_bytes": None, "rows_total": {}}),
    )
    assert sample.lag_bytes is None


def test_parse_readiness_computes_commits_per_second_from_two_samples() -> None:
    first = {"longest_xact_ms": 0, "lock_waiters": 0, "lag_bytes": 0, "xact_commit": 100}
    second = {"longest_xact_ms": 35, "lock_waiters": 1, "lag_bytes": 72, "xact_commit": 183}
    sample = q.parse_readiness(json.dumps(first), json.dumps(second), 2.0)
    assert (sample.longest_xact_ms, sample.lock_waiters, sample.lag_bytes) == (35, 1, 72)
    assert sample.commits_per_sec == 41.5
