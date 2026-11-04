"""트래픽 전환 판단과 실행 순서(#61).

프록시(Envoy)·관측은 포트로 분리했다. 실제 호출은 Temporal Activity가 구현을 넣고,
Workflow에는 외부 I/O를 두지 않는다. 롤백 뒤 자동 재시도·자동 복귀는 하지 않는다(사람이 확인).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from .contracts import (
    Handover,
    Precheck,
    Sample,
    ShiftResult,
    StepOutcome,
    TrafficPolicy,
)

ORIGIN, TARGET = "origin", "target"


class ProxyPort(Protocol):
    def apply(self, weights: dict[str, int]) -> None:
        """가중치를 적용하고 프록시가 그 버전을 받아들였다고 확인되면 돌아온다. 확인 실패는 예외."""


class ObserverPort(Protocol):
    def sample_origin(self, seconds: float) -> Sample: ...

    def sample_target(self, seconds: float) -> Sample: ...


def weights_for(percent: int) -> dict[str, int]:
    return {ORIGIN: 100 - percent, TARGET: percent}


def check_handover(handover: Handover) -> Precheck:
    """READY가 아니거나 도착지가 없으면 실행하지 않는다. UNKNOWN은 확인 전에 진행하지 않는다."""
    if handover.status != "READY":
        return Precheck(ok=False, reason=f"인계 상태가 READY가 아닙니다: {handover.status}")
    if not handover.ready:
        return Precheck(ok=False, reason="대상이 아직 ready가 아닙니다.")
    if not handover.external_endpoint:
        return Precheck(ok=False, reason="external_endpoint가 비어 있습니다.")
    return Precheck(ok=True)


def evaluate_step(
    policy: TrafficPolicy, baseline: Sample, observed: Sample, elapsed_s: float
) -> StepOutcome:
    """한 단계의 관측값으로 advance/hold/rollback을 판정한다(순수 함수)."""
    base = {"step": 0, "weights": {}}
    if observed.failed > policy.max_failed_requests:
        reason = f"실패 요청 {observed.failed}건 (허용 {policy.max_failed_requests}건)"
        return StepOutcome(**base, decision="rollback", reason=reason)
    limit = max(baseline.p99_ms * policy.max_p99_ratio, policy.p99_floor_ms)
    if observed.p99_ms > limit:
        reason = (
            f"p99 {observed.p99_ms:.0f}ms > 원본 {baseline.p99_ms:.0f}ms x {policy.max_p99_ratio}"
        )
        return StepOutcome(**base, decision="rollback", reason=reason)
    if observed.requests < policy.min_requests:
        if elapsed_s > policy.max_step_seconds:
            reason = f"요청 {observed.requests}건뿐이라 판단 불가, 단계 시간 초과"
            return StepOutcome(**base, decision="rollback", reason=reason)
        return StepOutcome(**base, decision="hold", reason="관측 요청 수 부족")
    return StepOutcome(**base, decision="advance")


class TrafficShifter:
    def __init__(
        self,
        proxy: ProxyPort,
        observer: ObserverPort,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.proxy, self.observer, self.sleep, self.clock = proxy, observer, sleep, clock

    def run(
        self, policy: TrafficPolicy, handover: Handover, db_gate_ok: bool | None = None
    ) -> ShiftResult:
        current = weights_for(0)
        events: list[StepOutcome] = []

        check = check_handover(handover)
        if not check.ok:
            return self._result("BLOCKED", current, events, 0, check.reason)
        try:
            baseline = self.observer.sample_origin(policy.observe_seconds)
        except Exception as exc:  # noqa: BLE001 - 포트 구현의 어떤 실패든 안전 쪽으로 처리
            return self._result("BLOCKED", current, events, 0, f"기준선 측정 실패: {exc}")

        for step in policy.steps:
            if step == policy.steps[-1] and policy.require_db_gate and db_gate_ok is not True:
                reason = "DB 게이트 미충족: 100% 단계 차단"
                return self._result("BLOCKED", current, events, step, reason)

            weights = weights_for(step)
            try:
                self.proxy.apply(weights)
            except Exception as exc:  # noqa: BLE001
                return self._rollback(events, step, f"설정 적용 confirm 실패: {exc}")

            began = self.clock()
            total = Sample(requests=0, failed=0, p99_ms=0)
            while True:
                try:
                    window = self.observer.sample_target(policy.observe_seconds)
                except Exception as exc:  # noqa: BLE001
                    return self._rollback(events, step, f"관측 실패: {exc}")
                # 요청이 적은 단계에서도 판단할 수 있도록 같은 단계 안의 관측값을 누적한다.
                total = Sample(
                    requests=total.requests + window.requests,
                    failed=total.failed + window.failed,
                    p99_ms=max(total.p99_ms, window.p99_ms),
                )
                outcome = evaluate_step(policy, baseline, total, self.clock() - began)
                if outcome.decision != "hold":
                    break
                self.sleep(policy.observe_seconds)

            if outcome.decision == "rollback":
                return self._rollback(events, step, outcome.reason)
            current = weights
            decision = "done" if step == policy.steps[-1] else "advance"
            events.append(StepOutcome(step=step, weights=weights, decision=decision))

        return ShiftResult(status="DONE", final_weights=current, events=events)

    # ------------------------------------------------------------ 내부
    @staticmethod
    def _result(
        status: str, weights: dict[str, int], events: list[StepOutcome], step: int, reason: str
    ) -> ShiftResult:
        decision = "rollback" if status in ("FAILED", "ROLLED_BACK") else "hold"
        events.append(StepOutcome(step=step, weights=weights, decision=decision, reason=reason))
        return ShiftResult(status=status, final_weights=weights, events=events)

    def _rollback(self, events: list[StepOutcome], step: int, reason: str) -> ShiftResult:
        safe = weights_for(0)
        try:
            self.proxy.apply(safe)
        except Exception as exc:  # noqa: BLE001
            return self._result("FAILED", safe, events, step, f"{reason}; rollback 실패: {exc}")
        return self._result("ROLLED_BACK", safe, events, step, reason)
