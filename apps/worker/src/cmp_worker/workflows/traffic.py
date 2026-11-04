"""트래픽 전환 자식 Workflow(#63). 판단은 순수 함수, 외부 호출은 Activity만 한다."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import MIGRATION_QUEUE
    from cmp_traffic import (
        Handover,
        Sample,
        TrafficPolicy,
        check_handover,
        evaluate_step,
        weights_for,
    )
    from cmp_traffic.snapshot import build_snapshot

    from cmp_worker.activities import traffic

ZERO = {"requests": 0, "failed": 0, "p99_ms": 0.0}


@workflow.defn
class TrafficShiftWorkflow:
    def __init__(self) -> None:
        self.cancel_requested = False
        self.snapshot: dict = {}
        self.events: list[dict] = []

    @workflow.signal
    def request_cancel(self) -> None:
        self.cancel_requested = True

    @workflow.query
    def status(self) -> dict:
        return {"snapshot": self.snapshot, "events": self.events}

    async def _call(self, fn, payload: dict, *, timeout_s: float, attempts: int) -> dict:
        return await workflow.execute_activity(
            fn,
            payload,
            task_queue=MIGRATION_QUEUE,
            schedule_to_start_timeout=timedelta(minutes=5),
            start_to_close_timeout=timedelta(seconds=timeout_s),
            retry_policy=RetryPolicy(maximum_attempts=attempts),
        )

    def _publish(self, policy, weights, step, baseline, observed, left, phase, **extra) -> None:
        self.snapshot = build_snapshot(
            phase=phase,
            policy=policy,
            weights=weights,
            step=step,
            baseline=baseline,
            observed=observed,
            observe_left_seconds=left,
            **extra,
        )

    def _result(self, status: str, weights: dict, reason: str = "") -> dict:
        if reason:
            self.events.append({"decision": "rollback", "reason": reason})
        return {"status": status, "final_weights": weights, "events": self.events}

    async def _rollback(self, policy, baseline, step, reason: str) -> dict:
        safe = weights_for(0)
        try:
            await self._call(traffic.traffic_rollback, {}, timeout_s=60, attempts=5)
        except ActivityError as exc:
            return self._result("FAILED", safe, f"{reason}; rollback 실패: {exc.cause}")
        self._publish(
            policy, safe, step, baseline, None, 0, "rolledback",
            decision="rollback", notes=[{"key": "gateFail", "params": {}}],
        )  # fmt: skip
        return self._result("ROLLED_BACK", safe, reason)

    @workflow.run
    async def run(self, request: dict) -> dict:
        policy = TrafficPolicy.model_validate(request.get("policy") or {})
        handover = Handover.model_validate(request["handover"])
        db_gate_ok = request.get("db_gate_ok")
        current = weights_for(0)

        check = check_handover(handover)
        if not check.ok:
            return self._result("BLOCKED", current, check.reason)
        try:
            prepared = await self._call(
                traffic.traffic_prepare,
                {"handover": handover.model_dump(), "policy": policy.model_dump()},
                timeout_s=policy.observe_seconds + 60,
                attempts=2,
            )
        except ActivityError as exc:
            return self._result("BLOCKED", current, f"준비 실패: {exc.cause}")
        baseline = Sample.model_validate(prepared["baseline"])

        for step in policy.steps:
            if step == policy.steps[-1] and policy.require_db_gate and db_gate_ok is not True:
                return self._result("BLOCKED", current, "DB 게이트 미충족: 100% 단계 차단")
            if self.cancel_requested:
                return await self._rollback(policy, baseline, step, "취소 요청")

            weights = weights_for(step)
            self._publish(policy, weights, step, baseline, None, policy.observe_seconds, "applying")
            try:
                await self._call(
                    traffic.traffic_set_weight, {"weights": weights}, timeout_s=60, attempts=2
                )
            except ActivityError as exc:
                return await self._rollback(
                    policy, baseline, step, f"설정 적용 confirm 실패: {exc.cause}"
                )

            began = workflow.time()
            total = Sample(requests=0, failed=0, p99_ms=0)
            while True:
                self._publish(
                    policy, weights, step, baseline, total, policy.observe_seconds, "observing"
                )
                try:
                    window = Sample.model_validate(
                        await self._call(
                            traffic.traffic_observe,
                            {"seconds": policy.observe_seconds},
                            timeout_s=policy.observe_seconds + 60,
                            attempts=2,
                        )
                    )
                except ActivityError as exc:
                    return await self._rollback(policy, baseline, step, f"관측 실패: {exc.cause}")
                total = Sample(
                    requests=total.requests + window.requests,
                    failed=total.failed + window.failed,
                    p99_ms=max(total.p99_ms, window.p99_ms),
                )
                outcome = evaluate_step(policy, baseline, total, workflow.time() - began)
                self._publish(policy, weights, step, baseline, total, 0, "gate")
                if outcome.decision != "hold":
                    break
                if self.cancel_requested:
                    return await self._rollback(policy, baseline, step, "취소 요청")

            if outcome.decision == "rollback":
                return await self._rollback(policy, baseline, step, outcome.reason)
            current = weights
            decision = "done" if step == policy.steps[-1] else "advance"
            self.events.append({"step": step, "decision": decision})
            self._publish(
                policy, weights, step, baseline, total, 0,
                "complete" if decision == "done" else "gate", decision=decision,
            )  # fmt: skip

        return {"status": "DONE", "final_weights": current, "events": self.events}
