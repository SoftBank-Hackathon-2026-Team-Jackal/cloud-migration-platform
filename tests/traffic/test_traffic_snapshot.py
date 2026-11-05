"""traffic.snapshot 페이로드(#63) 테스트. 프론트 계약(TrafficSnapshotData)과 필드를 맞춘다."""

from __future__ import annotations

from cmp_traffic import Sample, TrafficPolicy
from cmp_traffic.snapshot import build_snapshot

BASE = Sample(requests=100, failed=0, p99_ms=100)


def test_observing_snapshot_matches_the_frontend_contract_fields() -> None:
    snap = build_snapshot(
        phase="observing",
        policy=TrafficPolicy(),
        weights={"origin": 50, "target": 50},
        step=50,
        baseline=BASE,
        observed=Sample(requests=60, failed=1, p99_ms=150),
        observe_left_seconds=30,
    )
    assert set(snap) >= {"phase", "weights", "step", "gate", "residual"}
    assert snap["phase"] == "observing"
    assert snap["weights"] == {"origin": 50, "target": 50}
    assert snap["step"] == 50
    assert snap["gate"] == {
        "failed_requests": 1,
        "max_failed_requests": 0,
        "p99_ratio": 1.5,
        "max_p99_ratio": 1.5,
        "observe_left_seconds": 30,
    }
    assert "decision" not in snap


def test_gate_p99_ratio_is_relative_to_origin_and_zero_without_baseline() -> None:
    kwargs = {
        "phase": "gate",
        "policy": TrafficPolicy(),
        "weights": {"origin": 90, "target": 10},
        "step": 10,
        "observed": Sample(requests=40, failed=0, p99_ms=200),
        "observe_left_seconds": 0,
    }
    assert build_snapshot(baseline=BASE, **kwargs)["gate"]["p99_ratio"] == 2.0
    assert (
        build_snapshot(baseline=Sample(requests=0, failed=0, p99_ms=0), **kwargs)["gate"][
            "p99_ratio"
        ]
        == 0
    )


def test_rollback_snapshot_carries_decision_and_gate_fail_note() -> None:
    snap = build_snapshot(
        phase="rolledback",
        policy=TrafficPolicy(),
        weights={"origin": 100, "target": 0},
        step=50,
        baseline=BASE,
        observed=Sample(requests=60, failed=40, p99_ms=100),
        observe_left_seconds=0,
        decision="rollback",
        notes=[{"key": "gateFail", "params": {"failed": 40}}],
    )
    assert snap["decision"] == "rollback"
    assert snap["notes"] == [{"key": "gateFail", "params": {"failed": 40}}]


def test_snapshot_never_contains_endpoint_or_secret_fields() -> None:
    snap = build_snapshot(
        phase="applying",
        policy=TrafficPolicy(),
        weights={"origin": 90, "target": 10},
        step=10,
        baseline=BASE,
        observed=None,
        observe_left_seconds=120,
    )
    text = str(snap).lower()
    assert "endpoint" not in text and "secret" not in text and "token" not in text
    assert snap["gate"]["failed_requests"] == 0
