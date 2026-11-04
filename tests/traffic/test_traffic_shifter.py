"""트래픽 전환 제어 로직(#61) 단위 테스트. 프록시·관측은 가짜 포트로 대신한다."""

from __future__ import annotations

import pytest
from cmp_traffic import (
    Handover,
    Sample,
    ShiftResult,
    TrafficPolicy,
    TrafficShifter,
    check_handover,
    evaluate_step,
    weights_for,
)
from pydantic import ValidationError

GOOD = Sample(requests=100, failed=0, p99_ms=100)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProxy:
    def __init__(self, fail_on: list[int] | None = None, fail_rollback: bool = False) -> None:
        self.applied: list[dict[str, int]] = []
        self.fail_on = fail_on or []
        self.fail_rollback = fail_rollback

    def apply(self, weights: dict[str, int]) -> None:
        if weights["target"] in self.fail_on or (self.fail_rollback and weights["target"] == 0):
            raise RuntimeError("proxy did not confirm the new config")
        self.applied.append(weights)


class FakeObserver:
    def __init__(self, target: list[Sample], baseline: Sample = GOOD) -> None:
        self.baseline = baseline
        self.target = list(target)

    def sample_origin(self, seconds: float) -> Sample:
        return self.baseline

    def sample_target(self, seconds: float) -> Sample:
        return self.target.pop(0)


def shifter(proxy: FakeProxy, observer: FakeObserver, clock: FakeClock | None = None):
    clock = clock or FakeClock()
    return TrafficShifter(proxy, observer, sleep=clock.sleep, clock=clock)


READY = Handover(status="READY", ready=True, external_endpoint="203.0.113.10")


def test_weights_split_exactly_to_one_hundred() -> None:
    assert weights_for(10) == {"origin": 90, "target": 10}
    assert weights_for(100) == {"origin": 0, "target": 100}


def test_policy_steps_must_ascend_and_end_at_one_hundred() -> None:
    assert TrafficPolicy().steps == [10, 50, 100]
    for bad in ([50, 10, 100], [10, 10, 100], [0, 100], [10, 50], []):
        with pytest.raises(ValidationError):
            TrafficPolicy(steps=bad)


def test_all_gates_pass_advances_every_step_to_done() -> None:
    proxy = FakeProxy()
    result = shifter(proxy, FakeObserver([GOOD, GOOD, GOOD])).run(TrafficPolicy(), READY)
    assert result.status == "DONE"
    assert [w["target"] for w in proxy.applied] == [10, 50, 100]
    assert [e.decision for e in result.events] == ["advance", "advance", "done"]
    assert result.final_weights == {"origin": 0, "target": 100}


def test_failed_requests_over_limit_rolls_back_to_origin_immediately() -> None:
    proxy = FakeProxy()
    bad = Sample(requests=100, failed=3, p99_ms=100)
    result = shifter(proxy, FakeObserver([GOOD, bad, GOOD])).run(TrafficPolicy(), READY)
    assert result.status == "ROLLED_BACK"
    assert [w["target"] for w in proxy.applied] == [10, 50, 0]
    assert result.final_weights == {"origin": 100, "target": 0}
    assert result.events[-1].decision == "rollback"
    assert "3" in result.events[-1].reason


def test_slow_p99_rolls_back() -> None:
    slow = Sample(requests=100, failed=0, p99_ms=400)
    result = shifter(FakeProxy(), FakeObserver([slow, slow])).run(TrafficPolicy(), READY)
    assert result.status == "ROLLED_BACK"
    assert "p99" in result.events[-1].reason


def test_too_few_requests_never_advances_and_times_out_to_rollback() -> None:
    thin = Sample(requests=3, failed=0, p99_ms=50)
    decision = evaluate_step(TrafficPolicy(), GOOD, thin, elapsed_s=10)
    assert decision.decision == "hold"
    late = evaluate_step(TrafficPolicy(), GOOD, thin, elapsed_s=10_000)
    assert late.decision == "rollback"


def test_hold_is_retried_inside_the_step_until_data_is_enough() -> None:
    thin = Sample(requests=3, failed=0, p99_ms=50)
    proxy = FakeProxy()
    result = shifter(proxy, FakeObserver([thin, GOOD, GOOD, GOOD])).run(TrafficPolicy(), READY)
    assert result.status == "DONE"
    assert [w["target"] for w in proxy.applied] == [10, 50, 100]


def test_unconfirmed_apply_never_advances_and_rolls_back() -> None:
    proxy = FakeProxy(fail_on=[50])
    result = shifter(proxy, FakeObserver([GOOD, GOOD])).run(TrafficPolicy(), READY)
    assert result.status == "ROLLED_BACK"
    assert proxy.applied[-1] == {"origin": 100, "target": 0}
    assert "confirm" in result.events[-1].reason


def test_rollback_that_cannot_be_applied_is_reported_as_failed() -> None:
    proxy = FakeProxy(fail_rollback=True)
    bad = Sample(requests=100, failed=9, p99_ms=100)
    result = shifter(proxy, FakeObserver([bad])).run(TrafficPolicy(), READY)
    assert result.status == "FAILED"
    assert "rollback" in result.events[-1].reason


def test_baseline_failure_blocks_start_without_touching_the_proxy() -> None:
    class Broken(FakeObserver):
        def sample_origin(self, seconds: float) -> Sample:
            raise TimeoutError("origin unreachable")

    proxy = FakeProxy()
    result = shifter(proxy, Broken([GOOD])).run(TrafficPolicy(), READY)
    assert result.status == "BLOCKED"
    assert proxy.applied == []


@pytest.mark.parametrize(
    "handover",
    [
        Handover(status="FAILED", ready=False, external_endpoint=None),
        Handover(status="UNKNOWN", ready=True, external_endpoint="203.0.113.10"),
        Handover(status="READY", ready=False, external_endpoint="203.0.113.10"),
        Handover(status="READY", ready=True, external_endpoint=None),
        Handover(status="READY", ready=True, external_endpoint=""),
    ],
)
def test_handover_that_is_not_ready_blocks_without_calling_anything(handover) -> None:
    check = check_handover(handover)
    assert check.ok is False and check.reason
    proxy = FakeProxy()
    result = shifter(proxy, FakeObserver([GOOD])).run(TrafficPolicy(), handover)
    assert result.status == "BLOCKED"
    assert proxy.applied == []


def test_endpoint_with_embedded_credentials_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Handover(status="READY", ready=True, external_endpoint="http://user:secret@host")


def test_result_never_contains_the_endpoint_value() -> None:
    result = shifter(FakeProxy(), FakeObserver([GOOD, GOOD, GOOD])).run(TrafficPolicy(), READY)
    assert "203.0.113.10" not in result.model_dump_json()


def test_db_gate_blocks_the_final_step_when_required() -> None:
    proxy = FakeProxy()
    policy = TrafficPolicy(require_db_gate=True)
    result = shifter(proxy, FakeObserver([GOOD, GOOD])).run(policy, READY, db_gate_ok=False)
    assert result.status == "BLOCKED"
    assert [w["target"] for w in proxy.applied] == [10, 50]
    assert result.final_weights == {"origin": 50, "target": 50}
    assert "DB" in result.events[-1].reason


def test_db_gate_open_lets_final_step_through() -> None:
    policy = TrafficPolicy(require_db_gate=True)
    result = shifter(FakeProxy(), FakeObserver([GOOD] * 3)).run(policy, READY, db_gate_ok=True)
    assert result.status == "DONE"


def test_shift_result_is_a_plain_serializable_model() -> None:
    result = shifter(FakeProxy(), FakeObserver([GOOD] * 3)).run(TrafficPolicy(), READY)
    assert isinstance(result, ShiftResult)
    assert ShiftResult.model_validate_json(result.model_dump_json()) == result


def test_thin_windows_accumulate_inside_a_step_until_there_is_enough_data() -> None:
    thin = Sample(requests=12, failed=0, p99_ms=50)
    proxy = FakeProxy()
    observer = FakeObserver([thin, thin, thin, GOOD, GOOD])
    result = shifter(proxy, observer).run(TrafficPolicy(), READY)
    assert result.status == "DONE"  # 12+12+12=36건이 모여 첫 단계를 통과
    assert [w["target"] for w in proxy.applied] == [10, 50, 100]


def test_failures_inside_thin_windows_still_count() -> None:
    bad_thin = Sample(requests=12, failed=1, p99_ms=50)
    result = shifter(FakeProxy(), FakeObserver([bad_thin, bad_thin])).run(TrafficPolicy(), READY)
    assert result.status == "ROLLED_BACK"


def test_p99_ratio_is_ignored_below_the_absolute_floor() -> None:
    tiny_base = Sample(requests=100, failed=0, p99_ms=2)
    ok = Sample(requests=100, failed=0, p99_ms=90)  # 45배지만 하한 200ms 아래
    assert evaluate_step(TrafficPolicy(), tiny_base, ok, 1).decision == "advance"
    slow = Sample(requests=100, failed=0, p99_ms=300)
    assert evaluate_step(TrafficPolicy(), tiny_base, slow, 1).decision == "rollback"


def test_p99_gate_still_applies_with_the_absolute_floor_when_origin_had_no_baseline() -> None:
    no_base = Sample(requests=0, failed=0, p99_ms=0)
    slow = Sample(requests=100, failed=0, p99_ms=400)
    assert evaluate_step(TrafficPolicy(), no_base, slow, 1).decision == "rollback"
    fine = Sample(requests=100, failed=0, p99_ms=20)
    assert evaluate_step(TrafficPolicy(), no_base, fine, 1).decision == "advance"


def test_single_slow_window_is_an_outlier_not_a_rollback() -> None:
    slow = Sample(requests=100, failed=0, p99_ms=1000)
    proxy = FakeProxy()
    result = shifter(proxy, FakeObserver([slow, GOOD, GOOD, GOOD])).run(TrafficPolicy(), READY)
    assert result.status == "DONE"
    assert [w["target"] for w in proxy.applied] == [10, 50, 100]


def test_two_consecutive_slow_windows_roll_back() -> None:
    slow = Sample(requests=100, failed=0, p99_ms=1000)
    result = shifter(FakeProxy(), FakeObserver([slow, slow])).run(TrafficPolicy(), READY)
    assert result.status == "ROLLED_BACK"
    assert "p99" in result.events[-1].reason


def test_a_good_window_resets_the_slow_streak() -> None:
    slow = Sample(requests=100, failed=0, p99_ms=1000)
    observer = FakeObserver([slow, GOOD, slow, GOOD, GOOD, GOOD])
    result = shifter(FakeProxy(), observer).run(TrafficPolicy(), READY)
    assert result.status == "DONE"


def test_evaluate_step_without_streak_still_judges_immediately() -> None:
    slow = Sample(requests=100, failed=0, p99_ms=1000)
    assert evaluate_step(TrafficPolicy(), GOOD, slow, 1).decision == "rollback"
    assert evaluate_step(TrafficPolicy(), GOOD, slow, 1, p99_streak=1).decision == "hold"
    assert evaluate_step(TrafficPolicy(), GOOD, slow, 1, p99_streak=2).decision == "rollback"
