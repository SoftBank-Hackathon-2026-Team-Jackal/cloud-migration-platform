"""트래픽 전환 자식 Workflow(#63). 판단은 순수 함수, 외부 호출은 Activity만 한다.

#234: 웹 실제 모드용으로 `traffic.prepare`(사전 확인)·`traffic.ready`(전환 조건)를 내고, 사람이 누르는
롤백(`request_rollback`)·재시도(`request_retry`) Signal을 받는다. 자동 재시도·자동 복귀는 하지 않는다:
되돌린 뒤에는 사람이 재시도를 요청할 때까지(최대 `retry_wait_seconds`) 원본 100%로 기다린다.
"""

from __future__ import annotations

import re
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
    from cmp_traffic import (
        Handover,
        Sample,
        TrafficPolicy,
        check_handover,
        evaluate_step,
        p99_limit,
        weights_for,
    )
    from cmp_traffic.snapshot import (
        build_prepare,
        build_ready,
        build_snapshot,
        endpoint_check,
        handover_checks,
        prepare_failure,
    )

    from cmp_worker.activities import traffic

# #129: with a `migration_id` in the request every published snapshot is also stored as a
# `traffic.snapshot` event, so the web traffic screen shows the real shift (SSE).
EVENTS_PATCH = "traffic-events-v1"
# #234: traffic.prepare·traffic.ready events, manual rollback/retry Signals, wait for a retry.
ACTIONS_PATCH = "traffic-actions-v1"
EVENT_STATUS = {"complete": "done", "rolledback": "failed"}  # everything else is `running`
ZERO = {"requests": 0, "failed": 0, "p99_ms": 0.0}
QUEUE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SHIFTING = frozenset({"applying", "observing", "gate"})
RETRY_WAIT_SECONDS = 900
MANUAL_NOTE = {
    "key": "manual",
    "activity": "traffic_rollback",
    "params": {"origin": 100, "target": 0},
}


class _ManualRollback(Exception):
    """사람이 롤백을 요청해 관찰 중인 단계를 끊는다(Workflow 내부 흐름 제어용)."""


@workflow.defn
class TrafficShiftWorkflow:
    def __init__(self) -> None:
        self.cancel_requested = False
        self.rollback_requested = False
        self.retry_requested = False
        self.waiting_retry = False
        self.attempt = 1
        self.snapshot: dict = {}
        self.events: list[dict] = []
        self.queue = MIGRATION_QUEUE
        self.migration_id: str | None = None
        self.next_notes: list[dict] = []

    @workflow.signal
    def request_cancel(self) -> None:
        self.cancel_requested = True

    @workflow.signal
    def request_rollback(self) -> None:
        """#234: 사람이 누른 롤백. 전환 중(적용·관찰·판정)일 때만 받는다. 아니면 무시한다(멱등)."""
        if self.can_rollback():
            self.rollback_requested = True

    @workflow.signal
    def request_retry(self) -> None:
        """#234: 사람이 누른 재시도. 되돌린 뒤 기다리는 동안만 받는다. 아니면 무시한다(멱등)."""
        if self.waiting_retry:
            self.retry_requested = True

    def can_rollback(self) -> bool:
        return (
            self.snapshot.get("phase") in SHIFTING
            and not self.rollback_requested
            and not self.cancel_requested
            and not self.waiting_retry
        )

    @workflow.query
    def status(self) -> dict:
        return {
            "snapshot": self.snapshot,
            "events": self.events,
            "attempt": self.attempt,
            "actions": {
                "rollback": self.can_rollback(),
                "retry": self.waiting_retry and not self.retry_requested,
            },
        }

    async def _call(self, fn, payload: dict, *, timeout_s: float, attempts: int) -> dict:
        return await workflow.execute_activity(
            fn,
            payload,
            task_queue=self.queue,
            schedule_to_start_timeout=timedelta(minutes=5),
            start_to_close_timeout=timedelta(seconds=timeout_s),
            retry_policy=RetryPolicy(maximum_attempts=attempts),
        )

    async def _publish(
        self, policy, weights, step, baseline, observed, left, phase, status=None, **extra
    ) -> None:
        if self.next_notes:
            extra["notes"] = self.next_notes + list(extra.get("notes") or [])
            self.next_notes = []
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
        await self._emit(phase, status)

    async def _emit(self, phase: str, status: str | None = None) -> None:
        """Store the current snapshot as a web event. Observation only: it never changes the shift."""
        if not self.migration_id or not workflow.patched(EVENTS_PATCH):
            return
        await self._send(
            "traffic.snapshot", status or EVENT_STATUS.get(phase, "running"), self.snapshot
        )

    async def _send(self, event_type: str, status: str, data: dict, key: str | None = None) -> None:
        """One web event through `emit_migration_event` (the Workflow worker hosts it)."""
        request = {
            "migration_id": self.migration_id,
            "type": event_type,
            "phase": "traffic",
            "status": status,
            "data": data,
        }
        if key:
            request["idempotency_key"] = key
        try:
            await workflow.execute_activity(
                "emit_migration_event",
                request,
                task_queue=WORKFLOW_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            pass

    def _actions_on(self) -> bool:
        return bool(self.migration_id) and workflow.patched(ACTIONS_PATCH)

    async def _prepare_event(self, state: str, handover, policy, checks, reason=None) -> None:
        if not self._actions_on():
            return
        data = build_prepare(
            state=state, handover=handover, policy=policy, checks=checks, reason=reason
        )
        status = {"done": "done", "failed": "failed"}.get(state, "running")
        await self._send("traffic.prepare", status, data, key=f"traffic.prepare:{state}")

    async def _ready_event(self, state: str) -> None:
        """`traffic.ready` from the data migration's latest values; nothing when they are unknown."""
        if not self._actions_on():
            return
        try:
            readiness = await workflow.execute_activity(
                traffic.traffic_readiness,
                {"migration_id": self.migration_id},
                task_queue=WORKFLOW_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError:
            return
        data = build_ready(state, readiness or {})
        if data is not None:
            await self._send("traffic.ready", "running", data, key=f"traffic.ready:{state}")

    def _result(self, status: str, weights: dict, reason: str = "") -> dict:
        if reason:
            self.events.append({"decision": "rollback", "reason": reason})
        return {"status": status, "final_weights": weights, "events": self.events}

    async def _rollback(self, policy, baseline, step, reason: str, *, manual: bool = False) -> dict:
        safe = weights_for(0)
        try:
            await self._call(traffic.traffic_rollback, {}, timeout_s=60, attempts=5)
        except ActivityError as exc:
            return self._result("FAILED", safe, f"{reason}; rollback 실패: {exc.cause}")
        note = MANUAL_NOTE if manual else {"key": "gateFail", "params": {}}
        # 사람이 재시도할 수 있으면 아직 이전 실패가 아니다(웹은 `failed`를 이전 실패로 본다).
        retry_open = self._actions_on()
        await self._publish(
            policy, safe, step, baseline, None, 0, "rolledback",
            status="running" if retry_open else None, decision="rollback", notes=[note],
        )  # fmt: skip
        return self._result("ROLLED_BACK", safe, reason)

    async def _wait_retry(self, wait_seconds: float) -> bool:
        """되돌린 뒤 사람이 재시도를 요청하면 True. 기한이 지나거나 취소되면 False(자동 재시도 없음)."""
        self.waiting_retry, self.retry_requested = True, False
        try:
            await workflow.wait_condition(
                lambda: self.retry_requested or self.cancel_requested,
                timeout=timedelta(seconds=wait_seconds),
            )
        except TimeoutError:
            pass
        self.waiting_retry = False
        if self.retry_requested and not self.cancel_requested:
            self.retry_requested = self.rollback_requested = False
            self.attempt += 1
            self.next_notes = [{"key": "restart"}]
            return True
        return False

    async def _observe(self, seconds: float) -> dict:
        """관찰 창 하나. 사람이 롤백을 누르면 창이 끝나기를 기다리지 않는다."""
        if not self._actions_on():
            return await self._call(
                traffic.traffic_observe, {"seconds": seconds}, timeout_s=seconds + 60, attempts=2
            )
        handle = workflow.start_activity(
            traffic.traffic_observe,
            {"seconds": seconds},
            task_queue=self.queue,
            schedule_to_start_timeout=timedelta(minutes=5),
            start_to_close_timeout=timedelta(seconds=seconds + 60),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        await workflow.wait_condition(lambda: handle.done() or self.rollback_requested)
        if not handle.done():
            handle.cancel()
            raise _ManualRollback()
        return await handle

    @workflow.run
    async def run(self, request: dict) -> dict:
        # Envoy가 있는 중계 서버 호스트의 워커가 직접 받도록 전용 큐를 쓸 수 있다(없으면 기본 큐).
        queue = request.get("relay_queue") or MIGRATION_QUEUE
        if not isinstance(queue, str) or not QUEUE_NAME.fullmatch(queue):
            raise ApplicationError("invalid relay_queue name", non_retryable=True)
        self.queue = queue
        migration_id = request.get("migration_id")
        self.migration_id = migration_id if isinstance(migration_id, str) and migration_id else None
        policy = TrafficPolicy.model_validate(request.get("policy") or {})
        handover = Handover.model_validate(request["handover"])
        db_gate_ok = request.get("db_gate_ok")
        wait_seconds = float(request.get("retry_wait_seconds") or RETRY_WAIT_SECONDS)
        current = weights_for(0)

        await self._ready_event("running")
        check = check_handover(handover)
        if not check.ok:
            checks = handover_checks(handover)
            if all(item["state"] == "done" for item in checks):
                checks.append({"id": "external_endpoint", "state": "failed"})
            await self._prepare_event(
                "failed", handover, policy, checks, reason="HANDOVER_NOT_READY"
            )
            return self._result("BLOCKED", current, check.reason)
        await self._prepare_event(
            "running", handover, policy,
            handover_checks(handover) + [endpoint_check(handover, "running")],
        )  # fmt: skip
        try:
            prepared = await self._call(
                traffic.traffic_prepare,
                {"handover": handover.model_dump(), "policy": policy.model_dump()},
                timeout_s=policy.observe_seconds + 60,
                attempts=2,
            )
        except ActivityError as exc:
            cause = exc.cause
            error_type = cause.type if isinstance(cause, ApplicationError) else None
            checks, reason = prepare_failure(error_type, handover)
            await self._prepare_event("failed", handover, policy, checks, reason=reason)
            return self._result("BLOCKED", current, f"준비 실패: {exc.cause}")
        baseline = Sample.model_validate(prepared["baseline"])
        await self._prepare_event(
            "done", handover, policy, handover_checks(handover) + list(prepared.get("checks") or [])
        )
        gate_blocks = policy.require_db_gate and db_gate_ok is not True
        await self._ready_event("waiting" if gate_blocks else "done")

        while True:
            result = await self._shift(policy, baseline, db_gate_ok)
            if result["status"] != "ROLLED_BACK" or not self._actions_on():
                return result
            if not await self._wait_retry(wait_seconds):
                if not self.cancel_requested:
                    # 재시도 기한이 지났다: 이제 이전 실패로 알린다.
                    await self._emit("rolledback", "failed")
                return result

    async def _shift(self, policy, baseline, db_gate_ok) -> dict:
        current = weights_for(0)
        for step in policy.steps:
            if step == policy.steps[-1] and policy.require_db_gate and db_gate_ok is not True:
                return self._result("BLOCKED", current, "DB 게이트 미충족: 100% 단계 차단")
            if self.cancel_requested:
                return await self._rollback(policy, baseline, step, "취소 요청")
            if self.rollback_requested:
                return await self._rollback(policy, baseline, step, "수동 롤백", manual=True)

            weights = weights_for(step)
            await self._publish(
                policy, weights, step, baseline, None, policy.observe_seconds, "applying"
            )
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
            streak = 0
            while True:
                if self.rollback_requested:
                    return await self._rollback(policy, baseline, step, "수동 롤백", manual=True)
                await self._publish(
                    policy, weights, step, baseline, total, policy.observe_seconds, "observing"
                )
                try:
                    window = Sample.model_validate(await self._observe(policy.observe_seconds))
                except _ManualRollback:
                    return await self._rollback(policy, baseline, step, "수동 롤백", manual=True)
                except ActivityError as exc:
                    return await self._rollback(policy, baseline, step, f"관측 실패: {exc.cause}")
                streak = streak + 1 if window.p99_ms > p99_limit(policy, baseline) else 0
                total = Sample(
                    requests=total.requests + window.requests,
                    failed=total.failed + window.failed,
                    p99_ms=window.p99_ms,
                )
                outcome = evaluate_step(policy, baseline, total, workflow.time() - began, streak)
                await self._publish(policy, weights, step, baseline, total, 0, "gate")
                if outcome.decision != "hold":
                    break
                if self.cancel_requested:
                    return await self._rollback(policy, baseline, step, "취소 요청")

            if outcome.decision == "rollback":
                return await self._rollback(policy, baseline, step, outcome.reason)
            current = weights
            decision = "done" if step == policy.steps[-1] else "advance"
            self.events.append({"step": step, "decision": decision})
            await self._publish(
                policy, weights, step, baseline, total, 0,
                "complete" if decision == "done" else "gate", decision=decision,
            )  # fmt: skip

        return {"status": "DONE", "final_weights": current, "events": self.events}
