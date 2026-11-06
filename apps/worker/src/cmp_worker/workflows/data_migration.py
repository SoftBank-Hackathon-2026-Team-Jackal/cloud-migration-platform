"""데이터 이전 자식 Workflow(specs/data-migration, ticket 05). 외부 호출은 Activity만 한다.

흐름: (입력에 `tunnel`이 있으면) WireGuard 복제 경로 → 사전 점검 → 준비 → 스키마 → 구독 → streaming까지 감시 → PgBouncer 설치·앱 연결 →
(감시하며 신호 대기) → `switchover` 신호로 DB 전환 → `rollback` 신호로 되돌리기 / `finish` 신호로 끝.
`cancel` 신호는 전환 전이면 구독·발행을 지우고 끝낸다(원본 슬롯이 WAL을 붙잡지 않게).

감시는 짧은 Activity를 타이머 간격으로 반복한다(무기한 Activity 없음). 이력이 길어지지 않게 일정 횟수마다
continue-as-new로 상태를 넘긴다. Traffic의 DB 게이트는 `db_gate` Query로 읽는다(DM-007).

원본이 쓰기 주체이고 복제가 streaming이면 웹 두 번째 승인 화면용 `cutover.plan`과
`approval.required`(gate=cutover)를 낸다. 웹 승인(`POST /migrations/{id}/approve?gate=cutover`)은
API가 이 Workflow에 `switchover` 신호로 보낸다.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import MIGRATION_QUEUE

    from cmp_worker.activities import data_cutover as dc
    from cmp_worker.activities import data_migration as dm
    from cmp_worker.activities import data_tunnel as dt

MONITOR_ROUNDS_BEFORE_CONTINUE = 200
PARENT_SIGNAL = "data_migration_state"
ANNOUNCE_PATCH = "data-cutover-announce-v1"
TUNNEL_PATCH = "data-tunnel-v1"
SWITCH_FROM = {"switchover": "source", "rollback": "target"}


def _code(exc: ActivityError) -> str:
    cause = exc.cause
    return getattr(cause, "type", None) or "DATA_ACTIVITY_FAILED"


@workflow.defn
class DataMigrationWorkflow:
    def __init__(self) -> None:
        self.phase = "created"
        self.active = "source"  # 지금 쓰기 주체
        self.last_sample: dict = {}
        self.last_readiness: dict = {}
        self.switches: list[dict] = []
        self.failure_code: str | None = None
        self.requested: str | None = None  # switchover | rollback | finish | cancel
        self.last_steps: list[dict] | None = None  # 지난 전환의 단계별 시간(계획 화면에 보인다)
        self.announced: list | None = None  # 마지막으로 알린 [복제 준비됨, 전환 시도 수]
        self.tunnel: dict | None = None  # ticket 10: {name, endpoint, handshake, peer_host}

    # ---- 신호·조회 ----

    @workflow.signal
    def switchover(self) -> None:
        self.requested = "switchover"

    @workflow.signal
    def rollback(self) -> None:
        self.requested = "rollback"

    @workflow.signal
    def finish(self) -> None:
        self.requested = "finish"

    @workflow.signal
    def request_cancel(self) -> None:
        self.requested = "cancel"

    @workflow.query
    def status(self) -> dict:
        return {
            "phase": self.phase,
            "active": self.active,
            "last_sample": self.last_sample,
            "last_readiness": self.last_readiness,
            "switches": self.switches,
            "failure_code": self.failure_code,
            "tunnel": self.tunnel,
        }

    @workflow.query
    def db_gate(self) -> dict:
        """Traffic `require_db_gate`용: 쓰기 주체가 대상이고 역방향 복제가 살아 있는지."""
        sample = self.last_sample
        reverse_ok = (
            self.active == "target"
            and sample.get("phase") == "streaming"
            and not sample.get("stale")  # 감시에 실패한 값으로는 정상이라 하지 않는다
        )
        return {"writer": self.active, "reverse_replication_ok": reverse_ok}

    # ---- 실행 ----

    async def _call(self, fn, payload: dict, *, timeout_s: float, attempts: int = 3) -> dict:
        return await workflow.execute_activity(
            fn,
            payload,
            task_queue=MIGRATION_QUEUE,
            schedule_to_start_timeout=timedelta(minutes=5),
            start_to_close_timeout=timedelta(seconds=timeout_s),
            retry_policy=RetryPolicy(maximum_attempts=attempts),
        )

    def _req(self, args: dict) -> dict:
        direction = "forward" if self.active == "source" else "reverse"
        return args["request"] | {"direction": direction}

    def _cutover_payload(self, args: dict) -> dict:
        return {"request": self._req(args), "app": args["app"], "settings": args["settings"]}

    async def _monitor_once(self, args: dict) -> None:
        req = self._req(args)
        gate = self.db_gate()
        try:
            self.last_sample = await self._call(dm.dm_sample, req, timeout_s=60)
            self.last_readiness = await self._call(dm.dm_readiness, req, timeout_s=60)
        except ActivityError:
            # 감시 실패는 상태만 남기고 다음 주기에 다시 본다(전환·정리를 막지 않는다).
            self.last_sample = {**self.last_sample, "stale": True}
        if self.db_gate() != gate:
            await self._notify()  # 부모(Traffic DB 게이트)가 값이 바뀐 것을 알게 한다

    async def _replicate(self, args: dict) -> str | None:
        req = self._req(args)
        if args.get("tunnel") and workflow.patched(TUNNEL_PATCH):
            # ticket 10: 두 클러스터 사이 WireGuard 게이트웨이. LB 주소·handshake를 기다린다.
            self.phase = "tunnel"
            self.tunnel = await self._call(
                dt.dm_tunnel,
                {"request": req, "tunnel": args["tunnel"]},
                timeout_s=3600 if args["tunnel"].get("ingress") == "source" else 1800,
                attempts=2,
            )
        self.phase = "precheck"
        pre = await self._call(dm.dm_precheck, req, timeout_s=120)
        if not pre["ok"]:
            self.failure_code = "PRECHECK_BLOCKED"
            return "blocked"
        self.phase = "prepare"
        await self._call(dm.dm_prepare, {"request": req, "actions": pre["actions"]}, timeout_s=600)
        self.phase = "schema"
        await self._call(dm.dm_copy_schema, req, timeout_s=900)
        await self._call(dm.dm_subscribe, req, timeout_s=300)
        self.phase = "copy"
        deadline = workflow.now() + timedelta(seconds=args.get("sync_timeout_s", 6 * 3600))
        interval = args.get("sample_interval_s", 2)
        while True:
            self.last_sample = await self._call(dm.dm_sample, req, timeout_s=60)
            self.phase = self.last_sample["phase"]
            if self.phase == "streaming":
                break
            if self.requested == "cancel":
                return await self._cancel(args)
            if workflow.now() > deadline:
                self.failure_code = "SYNC_TIMEOUT"
                return "failed"
            await workflow.sleep(interval)
        await self._call(dc.dm_install_cutover, self._cutover_payload(args), timeout_s=600)
        await self._call(dc.dm_route_app, self._cutover_payload(args), timeout_s=600)
        return None

    async def _cancel(self, args: dict) -> str:
        self.phase = "cancelling"
        if self.active == "source":
            await self._call(dm.dm_cleanup, self._req(args), timeout_s=300, attempts=5)
        self.phase = "cancelled"
        return "cancelled"

    async def _announce(self, args: dict) -> None:
        """원본이 쓰기 주체일 때 전환 계획을 알린다. 복제 준비 상태나 시도 수가 바뀔 때만 다시 보낸다.
        새 시도(처음, 거부 뒤, 롤백 뒤)마다 승인 요청도 함께 보낸다. 알림 실패는 이전을 막지 않는다."""
        if self.active != "source" or not workflow.patched(ANNOUNCE_PATCH):
            return
        sample = self.last_sample
        key = [sample.get("phase") == "streaming" and not sample.get("stale"), len(self.switches)]
        if key == self.announced:
            return
        payload = {**self._cutover_payload(args), "sample": sample,
                   "readiness": self.last_readiness, "last_steps": self.last_steps,
                   "approval": self.announced is None or self.announced[1] != key[1]}  # fmt: skip
        try:
            await self._call(dc.dm_announce_cutover, payload, timeout_s=60)
        except ActivityError:
            return
        self.announced = key

    async def _switch(self, args: dict) -> None:
        going = "switching" if self.active == "source" else "rolling_back"
        self.phase = going
        payload = {**self._cutover_payload(args), "attempt": len(self.switches) + 1,
                   "readiness": self.last_readiness}  # fmt: skip
        try:
            # 전환은 한 번만 시도한다(재시도는 사람이 상태를 보고 다시 신호를 보낸다).
            result = await self._call(dc.dm_switchover, payload, timeout_s=300, attempts=1)
        except ActivityError as exc:
            self.switches.append({"switched": False, "code": _code(exc), "from": self.active})
            self.phase = "streaming" if self.active == "source" else "switched"
            await self._notify()
            return
        self.switches.append(
            {k: result.get(k) for k in ("switched", "active", "write_pause_ms", "verify_ok")}
        )
        self.active = result["active"]
        self.last_steps = result.get("steps") or self.last_steps
        self.phase = "switched" if self.active == "target" else "streaming"
        await self._notify()

    async def _notify(self) -> None:
        """부모(MigrationWorkflow)에 상태를 알린다. 부모가 없거나 이미 끝났으면 무시한다."""
        parent = workflow.info().parent
        if parent is None:
            return
        state = {"phase": self.phase, "active": self.active, "failure_code": self.failure_code,
                 "switches": len(self.switches), **self.db_gate()}  # fmt: skip
        try:
            await workflow.get_external_workflow_handle(parent.workflow_id).signal(
                PARENT_SIGNAL, state
            )
        except Exception:  # noqa: BLE001 - 부모가 끝났어도 데이터 이전은 계속한다
            workflow.logger.info("parent not signalled")

    async def _finish(self, status: str) -> dict:
        await self._notify()
        return self._result(status)

    @workflow.run
    async def run(self, args: dict) -> dict:
        """args: {request, app, settings, sample_interval_s?, monitor_interval_s?, sync_timeout_s?,
        state?(continue-as-new)}"""
        state = args.get("state")
        if state:
            self.phase, self.active = state["phase"], state["active"]
            self.switches, self.requested = state["switches"], state.get("requested")
            self.last_steps, self.announced = state.get("last_steps"), state.get("announced")
        else:
            try:
                outcome = await self._replicate(args)
            except ActivityError as exc:
                self.failure_code, self.phase = _code(exc), "failed"
                return await self._finish("failed")
            if outcome:
                self.phase = outcome
                return await self._finish(outcome)
            self.phase = "streaming"
            await self._notify()

        interval = args.get("monitor_interval_s", 5)
        for _ in range(MONITOR_ROUNDS_BEFORE_CONTINUE):
            request, self.requested = self.requested, None
            # 전환은 원본이 쓰기 주체일 때만, 롤백은 대상이 쓰기 주체일 때만 받는다.
            if SWITCH_FROM.get(request or "") == self.active:
                await self._switch(args)
            elif request == "cancel" and self.active == "source":
                return await self._finish(await self._cancel(args))
            elif request == "finish":
                self.phase = "done"
                return await self._finish("done")
            await self._monitor_once(args)
            await self._announce(args)
            try:
                await workflow.wait_condition(
                    lambda: self.requested is not None, timeout=timedelta(seconds=interval)
                )
            except TimeoutError:
                pass
        state = {"phase": self.phase, "active": self.active, "switches": self.switches,
                 "requested": self.requested, "last_steps": self.last_steps,
                 "announced": self.announced}  # fmt: skip
        workflow.continue_as_new({**args, "state": state})
        raise ApplicationError("unreachable")  # continue_as_new does not return

    def _result(self, status: str) -> dict:
        return {
            "status": status,
            "active": self.active,
            "switches": self.switches,
            "failure_code": self.failure_code,
        }
