"""Ticket 24: PoC panel/switchover values become valid web events."""

import json

from cmp_api.migration_events import InMemoryEmitter
from cmp_worker.poc_bridge import Bridge, state_events, switchover_events

STATE = {
    "t": 1730790000.1,
    "replication": {
        "phase": "catching_up",
        "lag": 2048,
        "tables": [
            {"name": "messages", "src": 81920, "dst": 40960, "state": "d", "copying_rows": 1200},
            {"name": "users", "src": 8192, "dst": 8192, "state": "r", "copying_rows": None},
        ],
    },
    "readiness": {
        "longest_xact_ms": 35,
        "open_xacts": 1,
        "lock_waiters": 0,
        "commits_per_sec": 41.5,
        "ready": True,
        "reasons": ["트랜잭션이 ..."],
    },
}
SWITCHOVER = {
    "from": "pg-a",
    "to": "pg-b",
    "impl": "fast",
    "write_pause_ms": 412,
    "verify_ok": True,
    "steps_ms": {
        "prepare_reverse (PAUSE 전)": 120,
        "seq_prebump (PAUSE 전)": 15,
        "sync_window_on (PAUSE 전)": 3,
        "wait_ready (PAUSE 전)": 900,
        "pause_wait_inflight": 80,
        "marker_wait": 150,
        "snapshot_seq_check": 30,
        "switch_subscriptions": 110,
        "retarget_resume": 40,
        "drop_forward (RESUME 후)": 60,
        "verify (RESUME 후)": 700,
    },
}


def test_panel_sample_maps_to_data_events():
    emitter = InMemoryEmitter()
    bridge = Bridge("poc-1", emitter)
    assert bridge.sample(STATE) == 5
    by_type = {e.type: e.data for e in emitter.events}
    assert by_type["data.phase"] == {"phase": "catchup"}
    assert by_type["data.lsn"]["lag_bytes"] == 2048
    assert by_type["data.readiness"] == {
        "longest_tx_ms": 35,
        "lock_waits": 0,
        "commits_per_sec": 41.5,
        "ready": True,
    }
    tables = [e.data for e in emitter.of_type("data.table")]
    assert [(t["name"], t["state"], t["rows_copied"]) for t in tables] == [
        ("messages", "copying", 1200),
        ("users", "copied", 0),
    ]
    assert "트랜잭션" not in json.dumps([e.data for e in emitter.events], ensure_ascii=False)
    assert bridge.sample(STATE) == 0  # unchanged values are not re-sent
    moved = {**STATE, "t": STATE["t"] + 1, "replication": {**STATE["replication"], "lag": 0}}
    assert bridge.sample(moved) == 1  # only the lag changed


def test_korean_metrics_phase_label_is_understood():
    drafts = state_events({"replication": {"phase": "실시간 동기화", "lag_bytes": 0}})
    assert drafts[0].data == {"phase": "streaming"}


def test_switchover_result_keeps_the_fast_order(tmp_path):
    drafts = switchover_events(SWITCHOVER)
    steps = [d.data for d in drafts if d.type == "cutover.step"]
    assert [s["id"] for s in steps] == [
        "prepare_reverse", "seq_raise", "sync_window_on", "wait_ready",  # before PAUSE
        "pause", "marker", "snapshot_seq_check", "sub_off", "reverse", "retarget",
        "drop_forward", "verify",
    ]  # fmt: skip
    assert steps[1]["window"] == "pre" and steps[-1]["of"] == len(steps)
    assert "window" not in steps[-1] and all("start_ms" not in s for s in steps)
    [outcome] = [d.data for d in drafts if d.type == "cutover.outcome"]
    timing = {s["id"]: s for s in outcome["steps"]}
    # subscription off/on run together: same start inside the pause window
    assert timing["sub_off"]["start_ms"] == timing["reverse"]["start_ms"] == 80 + 150 + 30
    assert timing["retarget"]["start_ms"] == 80 + 150 + 30 + 110
    assert outcome["result"] == "switched" and outcome["pause_ms"] == 412
    assert outcome["steps"][1] == {"id": "seq_raise", "ms": 15.0, "window": "pre"}

    emitter = InMemoryEmitter()
    (tmp_path / "switchover-20261105T120000-pg-a-to-pg-b.json").write_text(json.dumps(SWITCHOVER))
    bridge = Bridge("poc-1", emitter)
    assert bridge.results(tmp_path) == len(drafts)  # every draft passes the web contract
    assert bridge.results(tmp_path) == 0  # each file once
    last = emitter.events[-1]
    assert last.data["id"] == "checksum" and last.data["ok"] is True and last.status == "done"


def test_failed_switchover_is_a_refused_outcome_and_masked_log():
    emitter = InMemoryEmitter()
    stderr = "ERROR: postgresql://app:hunter22@pg-b/app CHECKSUM_MISMATCH on orders"
    Bridge("poc-1", emitter).failed(3, stderr)
    [outcome] = emitter.events
    assert outcome.type == "cutover.outcome" and outcome.status == "waiting"
    assert outcome.data["result"] == "refused" and outcome.data["active"] == "source"
    assert outcome.data["reason"] == "CHECKSUM_MISMATCH" and outcome.data["pause_ms"] is None
    assert "hunter22" not in json.dumps(outcome.data) + json.dumps(emitter.logs)
