"""데이터 이전(CDC) 판단 로직 단위 테스트. DB 없이 PoC(`poc/cdc`)에서 측정한 값 모양을 넣는다.

시험 경계: `cmp_data`의 공개 함수와, 웹 계약 검증 함수 `cmp_contracts.events.validate_data`.
"""

from __future__ import annotations

import pytest
from cmp_contracts.events import validate_data
from cmp_data import (
    PrecheckFacts,
    ReadinessPolicy,
    ReadinessSample,
    SyncSample,
    TableSync,
    evaluate_precheck,
    evaluate_readiness,
    readiness_event,
    replication_names,
    sync_events,
    sync_phase,
)

POLICY = ReadinessPolicy()


def table(name: str, state: str, rows_copied: int = 0, rows_total: int = 0) -> TableSync:
    return TableSync(name=name, state=state, rows_copied=rows_copied, rows_total=rows_total)


def facts(**overrides) -> PrecheckFacts:
    base = {
        "server_version_num": 160004,
        "wal_level": "logical",
        "max_replication_slots": 10,
        "slots_in_use": 0,
        "tables_without_identity": [],
        "sequences": ["public.messages_id_seq"],
        "large_objects": 0,
        "unlogged_tables": 0,
        "extensions": [],
    }
    return PrecheckFacts(**(base | overrides))


# ---- 사전 점검 ----


def test_precheck_passes_a_ready_source() -> None:
    result = evaluate_precheck(facts())
    assert result.ok
    assert result.blockers == []
    assert result.actions == []


def test_precheck_blocks_when_wal_level_is_not_logical() -> None:
    result = evaluate_precheck(facts(wal_level="replica"))
    assert not result.ok
    assert "WAL_LEVEL_NOT_LOGICAL" in result.blockers


def test_precheck_blocks_when_no_replication_slot_is_free() -> None:
    result = evaluate_precheck(facts(max_replication_slots=4, slots_in_use=4))
    assert not result.ok
    assert "NO_FREE_REPLICATION_SLOT" in result.blockers


def test_precheck_blocks_versions_without_logical_replication() -> None:
    assert "VERSION_UNSUPPORTED" in evaluate_precheck(facts(server_version_num=90624)).blockers


def test_tables_without_replica_identity_become_an_action_not_a_blocker() -> None:
    # PoC 시나리오 03: PK 없는 테이블은 UPDATE/DELETE 복제가 실패하므로 REPLICA IDENTITY FULL을 건다.
    result = evaluate_precheck(facts(tables_without_identity=["public.audit_log"]))
    assert result.ok
    assert result.actions == [("REPLICA_IDENTITY_FULL", "public.audit_log")]


def test_objects_logical_replication_skips_are_reported_as_warnings() -> None:
    result = evaluate_precheck(facts(large_objects=3, unlogged_tables=1, extensions=["pgcrypto"]))
    assert result.ok
    assert {"LARGE_OBJECTS_NOT_REPLICATED", "UNLOGGED_TABLES_NOT_REPLICATED"} <= set(
        result.warnings
    )
    assert "EXTENSIONS_NEED_TARGET_INSTALL" in result.warnings


# ---- 복제 객체 이름 ----


def test_replication_names_are_valid_postgres_identifiers_and_differ_by_direction() -> None:
    mid = "migration-3f2b9c1e-5d4a-4c8b-9e7f-0a1b2c3d4e5f"
    forward = replication_names(mid, "forward")
    reverse = replication_names(mid, "reverse")
    for names in (forward, reverse):
        for name in (names.publication, names.subscription, names.slot):
            assert len(name) <= 63
            assert name.replace("_", "").isalnum() and name == name.lower()
    assert forward.subscription != reverse.subscription
    assert forward == replication_names(mid, "forward")  # 재시도해도 같은 이름(멱등)


# ---- 복제 단계 ----


def test_phase_is_schema_before_the_subscription_has_tables() -> None:
    assert sync_phase(SyncSample(tables=[], lag_bytes=None), POLICY) == "schema"


def test_phase_is_copy_while_any_table_is_still_copying() -> None:
    sample = SyncSample(tables=[table("a", "r"), table("b", "d")], lag_bytes=0)
    assert sync_phase(sample, POLICY) == "copy"


def test_phase_is_catchup_until_every_table_is_ready_and_lag_is_small() -> None:
    not_ready = SyncSample(tables=[table("a", "r"), table("b", "s")], lag_bytes=0)
    lagging = SyncSample(tables=[table("a", "r")], lag_bytes=POLICY.lag_ok_bytes + 1)
    unknown_lag = SyncSample(tables=[table("a", "r")], lag_bytes=None)
    for sample in (not_ready, lagging, unknown_lag):
        assert sync_phase(sample, POLICY) == "catchup"


def test_phase_is_streaming_when_all_tables_are_ready_and_lag_is_within_limit() -> None:
    sample = SyncSample(tables=[table("a", "r")], lag_bytes=POLICY.lag_ok_bytes)
    assert sync_phase(sample, POLICY) == "streaming"


# ---- 전환 가능 시점 ----


def test_ready_when_no_long_transaction_no_lock_wait_and_small_lag() -> None:
    ready, reasons = evaluate_readiness(
        ReadinessSample(longest_xact_ms=50, lock_waiters=0, lag_bytes=10, commits_per_sec=40),
        POLICY,
    )
    assert ready and reasons == []


@pytest.mark.parametrize(
    ("sample", "code"),
    [
        (ReadinessSample(longest_xact_ms=201, lock_waiters=0, lag_bytes=0), "LONG_TRANSACTION"),
        (ReadinessSample(longest_xact_ms=0, lock_waiters=2, lag_bytes=0), "LOCK_WAIT"),
        (
            ReadinessSample(longest_xact_ms=0, lock_waiters=0, lag_bytes=2_000_000),
            "REPLICATION_LAG",
        ),
        (ReadinessSample(longest_xact_ms=0, lock_waiters=0, lag_bytes=None), "REPLICATION_OFF"),
    ],
)
def test_not_ready_reasons_are_codes(sample: ReadinessSample, code: str) -> None:
    ready, reasons = evaluate_readiness(sample, POLICY)
    assert not ready
    assert reasons == [code]


# ---- 웹 이벤트 ----


def test_sync_events_match_the_web_contract() -> None:
    sample = SyncSample(
        tables=[table("messages", "d", rows_copied=1200, rows_total=5000), table("rooms", "r")],
        lag_bytes=72,
        source_lsn="0/3000148",
        target_lsn="0/3000100",
    )
    events = sync_events(sample, POLICY)
    assert [t for t, _ in events] == ["data.phase", "data.lsn", "data.table", "data.table"]
    for event_type, data in events:
        validate_data(event_type, data)  # 웹 계약을 어기면 ValueError
    assert events[0][1] == {"phase": "copy"}
    assert events[2][1]["state"] == "copying"
    assert events[3][1]["state"] == "copied"


def test_unknown_lag_emits_no_lsn_event() -> None:
    events = sync_events(SyncSample(tables=[], lag_bytes=None), POLICY)
    assert [t for t, _ in events] == ["data.phase"]


def test_readiness_event_matches_the_web_contract() -> None:
    sample = ReadinessSample(longest_xact_ms=35, lock_waiters=0, lag_bytes=0, commits_per_sec=41.5)
    data = readiness_event(sample, POLICY)
    assert validate_data("data.readiness", data) == {
        "longest_tx_ms": 35,
        "lock_waits": 0,
        "commits_per_sec": 41.5,
        "ready": True,
    }
