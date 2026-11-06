import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    CancelledError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.handoff import (
        HANDOFF_UNAVAILABLE,
        TRAFFIC_HANDOFF_PATCH,
        TRAFFIC_HANDOVER_ACTIVITY,
        TRAFFIC_HANDOVER_HEARTBEAT_S,
        TRAFFIC_HANDOVER_START_TO_CLOSE_S,
        TRAFFIC_HANDOVER_WAIT_PATCH,
        TRAFFIC_SHIFT_PATCH,
    )
    from cmp_contracts.migration import FAILURE_OUTCOMES, OUTCOME_PHASE, WEB_PHASE
    from cmp_contracts.plan_publish import (
        DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        PLAN_PATCH,
        WORKLOAD_PATCH,
    )
    from cmp_contracts.queues import (
        ANALYSIS_QUEUE,
        APPROVAL_WORKFLOW_NAME,
        BUILD_QUEUE,
        MIGRATION_QUEUE,
        PROVISIONING_QUEUE,
        SANDBOX_QUEUE,
        WORKFLOW_QUEUE,
        approval_workflow_id,
    )
    from cmp_data.handoff import DataMigrationInput, endpoints
    from cmp_traffic import TrafficShiftInput

    from cmp_worker.activities.analysis import analyze_project
    from cmp_worker.activities.build import prepare_image
    from cmp_worker.activities.migration import copy_data, shift_traffic, verify_target
    from cmp_worker.activities.provisioning import prepare_plan, provision_target
    from cmp_worker.workflows.analysis import AnalysisWorkflow
    from cmp_worker.workflows.data_migration import DataMigrationWorkflow
    from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
    from cmp_worker.workflows.traffic import TrafficShiftWorkflow

# ENV-02: the start input may carry `env_file_ref`; it is copied into the analysis, Sandbox and
# Planning inputs. Only the reference travels; values stay in the encrypted store.
ENV_FILE_REF_PATCH = "env-file-ref-v1"

# Ticket 49 F6: a failed run reports one `error` event and a `MIGRATION_FAILED` log.
FAILURE_EVENT_PATCH = "failure-event-v1"
# F7: reporting is observation; it never changes the outcome (events E9).
REPORT_ACTIVITY = {
    "task_queue": WORKFLOW_QUEUE,  # the Workflow worker also hosts `emit_migration_event`
    "start_to_close_timeout": timedelta(seconds=30),
    "schedule_to_close_timeout": timedelta(minutes=2),
    "retry_policy": RetryPolicy(maximum_attempts=3),
}
DATA_PATCH = "data-migration-v1"
# 데이터 이전 자식이 이 상태가 되면 부모가 다음 단계(트래픽)로 간다. DB를 먼저 전환한다(DM-007).
# #128: TrafficShiftWorkflow result status -> MigrationWorkflow outcome (None: go on to verification).
TRAFFIC_DONE = {
    "DONE": None,
    "ROLLED_BACK": ("traffic_rolled_back", "TRAFFIC_ROLLED_BACK"),
    "BLOCKED": ("traffic_blocked", "TRAFFIC_BLOCKED"),
    "FAILED": ("traffic_failed", "TRAFFIC_FAILED"),
}
DATA_DONE = {"switched": None, "blocked": "data_blocked", "failed": "data_failed",
             "cancelled": "cancelled", "done": None}  # fmt: skip

# Ticket 60: the web's "request a plan change" is a REJECT; the Migration republishes (the
# publisher never reuses a REJECTed Revision) and waits for the new Revision's approval.
REJECT_REPUBLISH_PATCH = "plan-reject-republish-v1"
MAX_REJECTED_REVISIONS = 3  # the third REJECT ends the Migration `plan_rejected` as before

PUBLISH_ACTIVITY = {
    "task_queue": PROVISIONING_QUEUE,
    "start_to_close_timeout": timedelta(hours=1),
    "heartbeat_timeout": timedelta(minutes=5),
    "retry_policy": RetryPolicy(maximum_attempts=3),  # publication is input-idempotent
}


@workflow.defn
class MigrationWorkflow:
    def __init__(self) -> None:
        self.stage = "created"  # ticket 49 F4: last in-progress phase (where a failure happened)
        self.phase = "created"
        self.approved = False
        self.cancel_requested = False
        self.plan: dict | None = None
        self.plan_code: str | None = None
        self.infrastructure_plan: dict | None = None  # ticket 22: executed AWS/GCP infra Plan
        self.review_pending = False
        self.failure_code: str | None = None
        self.pipeline: dict | None = None  # ticket 21: references the real pipeline produced
        self.env_file_ref: dict | None = None  # ENV-02: {id, version, sha256}, never values
        self.failure_phase: str | None = None  # ticket 49: web phase of the failure
        self.failure_issues: list[str] = []  # ticket 49 F3: detailed codes (Plan issues)
        self.data_migration: dict | None = None  # specs/data-migration: child state (no secrets)
        # Ticket 50 H5: `TrafficHandoff` read after a successful execution, for the traffic step
        # (#128 passes `handover` to TrafficShiftWorkflow as is). No secret values.
        self.traffic_handoff: dict | None = None
        # #128: TrafficShiftWorkflow outcome {status, final_weights}. Codes and numbers only.
        self.traffic_shift: dict | None = None

    @property
    def phase(self) -> str:
        return self._phase

    @phase.setter
    def phase(self, value: str) -> None:
        if value in WEB_PHASE:
            self.stage = value
        self._phase = value

    def db_gate_ok(self) -> bool | None:
        """Traffic(#128) `require_db_gate`용(DM-007): DB 쓰기 주체가 대상이고 역방향 복제가 정상이면
        True. 데이터 이전을 이 실행에서 하지 않았으면 None(판단할 근거 없음)."""
        state = self.data_migration
        if state is None or "writer" not in state:
            return None
        return state["writer"] == "target" and bool(state.get("reverse_replication_ok"))

    @workflow.signal
    def data_migration_state(self, state: dict) -> None:
        """DataMigrationWorkflow가 단계가 바뀔 때 보낸다."""
        self.data_migration = state

    @workflow.signal
    def sandbox_review_pending(self, pending: bool) -> None:
        """The Sandbox child waits for (or no longer waits for) a person; not a terminal state."""
        self.review_pending = bool(pending)

    @workflow.signal
    def approve(self) -> None:
        self.approved = True

    @workflow.signal
    def request_cancel(self) -> None:
        self.cancel_requested = True

    @workflow.query
    def status(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "approval_pending": self.phase == "awaiting_approval",
            "cancel_requested": self.cancel_requested,
            "mode": "scaffold",
            "plan": self.plan,  # ticket 18: published Plan Revision identifiers only
            "plan_code": self.plan_code,
            "infrastructure_plan": self.infrastructure_plan,
            "review_pending": self.review_pending,  # ticket 14B: in progress, not terminal
            "failure_code": self.failure_code,
            "failure_phase": self.failure_phase,
            "failure_issues": self.failure_issues,
            "pipeline": self.pipeline,
            "env_file_ref": self.env_file_ref,
            "data_migration": self.data_migration,
            "traffic_handoff": self.traffic_handoff,
            "traffic_shift": self.traffic_shift,
        }

    async def _hand_off(
        self, planning: dict, analysis: dict, sandbox: dict, sandbox_input: dict
    ) -> str | None:
        """Assemble and register the Planning input. Returns a refusal code, or None when done."""
        self.phase = "planning_handoff"
        assembled = await workflow.execute_activity(
            "sbx_plan_assemble",
            {
                "migration_id": workflow.info().workflow_id,
                "settings": planning,
                "analysis": analysis,
                "sandbox": sandbox,
                "suite_ref": sandbox.get("suite_ref") or sandbox_input["suite_ref"],
            },
            task_queue=SANDBOX_QUEUE,
            schedule_to_start_timeout=timedelta(minutes=5),
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        if assembled["status"] != "READY":
            return assembled["code"]
        registered = await workflow.execute_activity(
            "register_planning_input",
            {
                "migration_id": workflow.info().workflow_id,
                "registration_ref": assembled["registration_ref"],
            },
            **PUBLISH_ACTIVITY,
        )
        if registered["status"] != "REGISTERED":
            return registered["code"]
        if self.pipeline is not None:
            self.pipeline = {**self.pipeline, "planning_input_digest": registered["input_digest"]}
        return None

    async def _plan_and_provision(self) -> str | None:
        """Ticket 18 P7: publish the Plan, wait for its Revision approval child, return the outcome.

        None means planning is not configured (scaffold path). Otherwise a terminal phase, or
        "executed" when the approved provider execution succeeded. Ticket 22 W8: after an AWS/GCP
        infrastructure execution, the WORKLOADS Plan for 09/11 goes through the same approval.
        """
        self.phase = "planning"
        request = {"migration_id": workflow.info().workflow_id}
        published = await workflow.execute_activity(
            "publish_migration_plan", request, **PUBLISH_ACTIVITY
        )
        if published["status"] == "SCAFFOLD":
            return None
        outcome = await self._approve_revisions(published, "publish_migration_plan", request)
        cloud_infra = published.get("provider") in ("AWS", "GCP") and (
            published.get("scope") == "INFRASTRUCTURE"
        )
        if outcome != "executed" or not cloud_infra or not workflow.patched(WORKLOAD_PATCH):
            return outcome
        if self.cancel_requested:
            return "cancelled"
        self.infrastructure_plan = self.plan
        self.phase = "workload_planning"
        request = {"migration_id": workflow.info().workflow_id, "infrastructure": self.plan}
        workloads = await workflow.execute_activity(
            "publish_workload_plan", request, **PUBLISH_ACTIVITY
        )
        return await self._approve_revisions(workloads, "publish_workload_plan", request)

    async def _approve_revisions(self, published: dict, activity_name: str, request: dict) -> str:
        """Ticket 60: after a REJECT, publish again and wait for the new Revision's approval.

        Ends `plan_rejected` as before when the republish returns the rejected Revision (policy
        reused it), after MAX_REJECTED_REVISIONS, on cancel, or for histories before the patch."""
        phase, rejections = self.phase, 0
        while True:
            outcome = await self._approve_and_execute(published)
            if outcome != "plan_rejected" or self.cancel_requested:
                return outcome
            if not workflow.patched(REJECT_REPUBLISH_PATCH):
                return outcome
            rejections += 1
            if rejections >= MAX_REJECTED_REVISIONS:
                return outcome
            rejected = (self.plan["plan_id"], self.plan["revision"])
            self.phase = phase
            published = await workflow.execute_activity(activity_name, request, **PUBLISH_ACTIVITY)
            if published.get("status") == "PUBLISHED" and rejected == (
                published.get("plan_id"),
                published.get("revision"),
            ):
                self.plan_code = "PLAN_REJECTED"  # the rejected Revision came back: stop
                return "plan_rejected"

    async def _approve_and_execute(self, published: dict) -> str:
        """Wait for the published Revision's approval child; return a terminal phase/"executed"."""
        self.plan_code = published["code"]
        if published["status"] != "PUBLISHED":
            self.failure_issues = list(published.get("issues") or [])  # ticket 49 F3
            return "plan_failed"
        self.plan = {"plan_id": published["plan_id"], "revision": published["revision"]}
        if self.cancel_requested:
            return "cancelled"
        self.phase = "awaiting_approval"
        try:
            # Same ID the approval API's Signal-With-Start targets: delivery signals this child.
            child = await workflow.start_child_workflow(
                APPROVAL_WORKFLOW_NAME,
                {**self.plan, "approval_timeout_seconds": DEFAULT_APPROVAL_TIMEOUT_SECONDS},
                id=approval_workflow_id(self.plan["plan_id"], self.plan["revision"]),
                task_queue=WORKFLOW_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
            )
        except WorkflowAlreadyStartedError:
            self.plan_code = "APPROVAL_WORKFLOW_EXISTS"
            return "provisioning_failed"
        cancel_wait = asyncio.create_task(workflow.wait_condition(lambda: self.cancel_requested))
        try:
            await workflow.wait([child, cancel_wait], return_when=asyncio.FIRST_COMPLETED)
            if not child.done():
                # Before a claim the child ends without a marker; after it, it shields DB steps.
                child.cancel()
            try:
                outcome = await asyncio.shield(child)
            except ChildWorkflowError:
                self.plan_code = "APPROVAL_WORKFLOW_FAILED"
                return "provisioning_failed"
        finally:
            cancel_wait.cancel()
        self.plan_code = outcome["code"]
        status = outcome["status"]
        if status == "EXECUTED":
            return "cancelled" if self.cancel_requested else "executed"
        if status == "CANCELLED":
            return "cancelled"
        return {"REJECTED": "plan_rejected", "EXPIRED": "approval_expired"}.get(
            status, "provisioning_failed"
        )

    async def _data_migration(self, given: dict) -> str | None:
        """specs/data-migration ticket 05: 인계값으로 대상 DB를 정하고 DataMigrationWorkflow를 시작한 뒤,
        DB가 대상으로 전환될 때까지 기다린다. None이면 다음 단계로 간다.

        전환 신호(`switchover`)는 자식 Workflow(`data-<migration_id>`)에 운영자가 보낸다. 자식은 부모가
        끝나도 남아 역방향 복제(롤백 대비)를 유지한다(ABANDON).
        """
        self.phase = "data_copy"
        parsed = DataMigrationInput.model_validate(given)
        view = await workflow.execute_activity(
            "get_migration_handoff",
            {**(self.plan or {}), "consumer": "DATA_MIGRATION"},
            task_queue=PROVISIONING_QUEUE,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        migration_id = workflow.info().workflow_id
        # ticket 10: `tunnel`이 있으면 양쪽 peer_host가 플랫폼 WireGuard 게이트웨이 주소가 된다
        source, target, code = endpoints(view, parsed, migration_id)
        if source is None or target is None:
            self.failure_code = code
            self.data_migration = {"phase": "blocked", "failure_code": code}
            return "data_blocked"
        child_args = {
            "request": {
                "migration_id": migration_id,
                "source": source.model_dump(mode="json"),
                "target": target.model_dump(mode="json"),
            },
            "app": parsed.app,
            "settings": parsed.settings,
            "sample_interval_s": parsed.sample_interval_s,
            "monitor_interval_s": parsed.monitor_interval_s,
        }
        if parsed.tunnel is not None:
            child_args["tunnel"] = parsed.tunnel.model_dump(mode="json")
        child = await workflow.start_child_workflow(
            DataMigrationWorkflow.run,
            child_args,
            id=f"data-{migration_id}",
            task_queue=WORKFLOW_QUEUE,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )

        def settled() -> bool:
            return self.cancel_requested or (self.data_migration or {}).get("phase") in DATA_DONE

        waiting = asyncio.create_task(workflow.wait_condition(settled))
        try:
            await workflow.wait([child, waiting], return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiting.cancel()
        if self.cancel_requested:
            await child.signal(DataMigrationWorkflow.request_cancel)
            return "cancelled"
        if child.done():
            try:
                result = child.result()
            except ChildWorkflowError:
                self.failure_code = "DATA_MIGRATION_FAILED"
                return "data_failed"
            self.data_migration = {**(self.data_migration or {}), "phase": result["status"]}
        phase = (self.data_migration or {}).get("phase")
        if DATA_DONE.get(phase):
            self.failure_code = (self.data_migration or {}).get("failure_code") or phase.upper()
            return DATA_DONE[phase]
        return None

    async def _read_traffic_handover(self) -> str | None:
        """Ticket 50 H4/H6: read the executed Revision's Traffic handover; None means go on.

        A blocked view or a failed lookup ends the run as `traffic_blocked` before any traffic
        change (#128 acceptance: no shift without a handover). The first code is the cause.
        """
        self.phase = "traffic"
        # Ticket 67: an ON_PREM lookup waits inside the Activity for the tunnel health check.
        timeouts = (
            {
                "start_to_close_timeout": timedelta(seconds=TRAFFIC_HANDOVER_START_TO_CLOSE_S),
                "heartbeat_timeout": timedelta(seconds=TRAFFIC_HANDOVER_HEARTBEAT_S),
            }
            if workflow.patched(TRAFFIC_HANDOVER_WAIT_PATCH)
            else {"start_to_close_timeout": timedelta(minutes=1)}
        )
        try:
            got = await workflow.execute_activity(
                TRAFFIC_HANDOVER_ACTIVITY,
                {"plan_id": self.plan["plan_id"], "revision": self.plan["revision"]},
                task_queue=PROVISIONING_QUEUE,
                schedule_to_start_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
                **timeouts,
            )
        except ActivityError:
            self.failure_code = HANDOFF_UNAVAILABLE
            return "traffic_blocked"
        self.traffic_handoff = got
        if got.get("proceed"):
            return None
        blocked = list(got.get("blocked_by") or []) or [HANDOFF_UNAVAILABLE]
        self.failure_code, self.failure_issues = blocked[0], blocked
        return "traffic_blocked"

    async def _shift_traffic(self, given: dict) -> str | None:
        """#128: 인계값으로 TrafficShiftWorkflow(자식)를 시작해 전환이 끝나거나 되돌아갈 때까지 기다린다.

        None이면 전환이 끝나 검증 단계로 간다. 되돌아갔거나(롤백)·막혔거나·실패하면 그 결과가 이 실행의
        종료 상태가 된다. 취소하면 자식에게 취소를 보내 원본으로 되돌린 뒤 끝난다.
        """
        self.phase = "traffic"
        parsed = TrafficShiftInput.model_validate(given)
        handoff = self.traffic_handoff or {}
        child = await workflow.start_child_workflow(
            TrafficShiftWorkflow.run,
            {
                "handover": handoff.get("handover") or {},
                "policy": parsed.policy.model_dump(mode="json"),
                "relay_queue": parsed.relay_queue,
                "migration_id": workflow.info().workflow_id,  # #129: the web event stream key
                # 입력으로 정하지 않았으면 이 실행의 데이터 이전 상태(DM-007)로 판단한다.
                "db_gate_ok": parsed.db_gate_ok
                if parsed.db_gate_ok is not None
                else self.db_gate_ok(),
            },
            id=f"traffic-{workflow.info().workflow_id}",
            task_queue=WORKFLOW_QUEUE,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        waiting = asyncio.create_task(workflow.wait_condition(lambda: self.cancel_requested))
        try:
            await workflow.wait([child, waiting], return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiting.cancel()
        if self.cancel_requested and not child.done():
            await child.signal(
                TrafficShiftWorkflow.request_cancel
            )  # the child rolls back to the source
            try:
                self.traffic_shift = {k: v for k, v in (await child).items() if k != "events"}
            except ChildWorkflowError:
                pass
            return "cancelled"
        try:
            result = await child
        except ChildWorkflowError:
            self.failure_code = "TRAFFIC_SHIFT_FAILED"
            return "traffic_failed"
        self.traffic_shift = {k: v for k, v in result.items() if k != "events"}
        mapped = TRAFFIC_DONE.get(str(result.get("status")), ("traffic_failed", "TRAFFIC_FAILED"))
        if mapped is None:
            return None
        outcome, code = mapped
        self.failure_code = code
        return outcome

    async def _step(self, name: str, fn: object, project_id: str, queue: str) -> None:
        if self.cancel_requested:
            self.phase = "cancelled"
            return
        self.phase = name
        await workflow.execute_activity(
            fn,
            project_id,
            task_queue=queue,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

    @workflow.run
    async def run(self, data: str | dict) -> str:
        """Ticket 49: `_run` plus failure reporting. The result string is unchanged (F5)."""
        try:
            outcome = await self._run(data)
        except Exception as exc:
            if is_cancelled_exception(exc) or not workflow.patched(FAILURE_EVENT_PATCH):
                raise
            self._record_failure("failed", _exception_code(exc))
            self.phase = "failed"
            await self._report_failure("failed")
            raise  # the Temporal execution stays Failed
        if outcome in FAILURE_OUTCOMES and workflow.patched(FAILURE_EVENT_PATCH):
            self._record_failure(outcome, self.plan_code)
            await self._report_failure(outcome)
        return outcome

    def _record_failure(self, outcome: str, code: str | None) -> None:
        """F2/F4: keep the first known code; the phase is where the run stopped."""
        self.failure_code = self.failure_code or code or outcome.upper()
        self.failure_phase = OUTCOME_PHASE.get(outcome) or WEB_PHASE[self.stage]

    async def _report_failure(self, outcome: str) -> None:
        """F1/F3/F7: one web `error` event and an operational log; never raises."""
        code, phase = self.failure_code, self.failure_phase
        try:
            await workflow.execute_activity(
                "emit_migration_event",
                {
                    "migration_id": workflow.info().workflow_id,
                    "type": "error",
                    "phase": phase,
                    "status": "failed",
                    "data": {"code": code, "message": "", "phase": phase, "retryable": False},
                    "idempotency_key": f"failure:{code}",
                    "log": {
                        "level": "error",
                        "code": "MIGRATION_FAILED",
                        "fields": {
                            "outcome": outcome,
                            "code": code,
                            "phase": phase,
                            "issues": self.failure_issues,
                        },
                    },
                },
                **REPORT_ACTIVITY,
            )
        except ActivityError:
            workflow.logger.warning("failure event not stored", extra={"code": code})

    async def _run(self, data: str | dict) -> str:
        # A string is the project ID of the scaffold run. A dict with "analysis" is the real
        # pipeline input: {"project_id", "analysis": AnalysisInput, "sandbox": SandboxSettings,
        # "planning"?, "env_file_ref"?} (tickets 21, 15, ENV-02). A dict without "analysis" is
        # the API's scaffold start that only adds `env_file_ref`.
        project_id = data if isinstance(data, str) else data["project_id"]
        start = None if isinstance(data, str) else data
        pipeline_input = start if start is not None and "analysis" in start else None
        env_ref = (start or {}).get("env_file_ref")
        if env_ref is not None and workflow.patched(ENV_FILE_REF_PATCH):
            self.env_file_ref = env_ref
        with_ref = {"env_file_ref": self.env_file_ref} if self.env_file_ref else {}
        analysis_result: dict | None = None
        if workflow.patched("analysis-child-v1"):
            if self.cancel_requested:
                self.phase = "cancelled"
                return "cancelled"
            self.phase = "analysis"
            migration_id = workflow.info().workflow_id
            child = await workflow.start_child_workflow(
                AnalysisWorkflow.run,
                (
                    migration_id
                    if pipeline_input is None
                    else {**pipeline_input["analysis"], "migration_id": migration_id, **with_ref}
                ),
                id=f"analysis-{migration_id}",
                task_queue=WORKFLOW_QUEUE,
                parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
            )
            cancel_wait = asyncio.create_task(
                workflow.wait_condition(lambda: self.cancel_requested)
            )
            child_cancel_requested = False
            try:
                await workflow.wait([child, cancel_wait], return_when=asyncio.FIRST_COMPLETED)
                if self.cancel_requested:
                    child_cancel_requested = True
                    child.cancel()
                    try:
                        await asyncio.shield(child)
                    except ChildWorkflowError as exc:
                        if not isinstance(exc.cause, CancelledError):
                            raise
                    self.phase = "cancelled"
                    return "cancelled"
                analysis_result = await child
            except asyncio.CancelledError:
                if not child_cancel_requested:
                    child.cancel()
                raise
            finally:
                cancel_wait.cancel()
        provisioned: str | None = None
        for name, fn, queue in (
            ("analysis", analyze_project, ANALYSIS_QUEUE),
            ("build", prepare_image, BUILD_QUEUE),
            ("planning", prepare_plan, PROVISIONING_QUEUE),
        ):
            if name == "analysis" and workflow.patched("analysis-child-v1"):
                continue
            if name == "build" and workflow.patched("sandbox-child-v1"):
                if self.cancel_requested:
                    self.phase = "cancelled"
                    return "cancelled"
                self.phase = "sandbox"
                sandbox_arg: str | dict = workflow.info().workflow_id
                if pipeline_input is not None and analysis_result is not None:
                    # The Sandbox loop input is built next to the analysis workspace and store.
                    sandbox_settings = pipeline_input.get("sandbox")
                    if with_ref:
                        sandbox_settings = {**(sandbox_settings or {}), **with_ref}
                    sandbox_arg = await workflow.execute_activity(
                        "assemble_sandbox_input",
                        {"analysis": analysis_result, "settings": sandbox_settings},
                        task_queue=ANALYSIS_QUEUE,
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                child = await workflow.start_child_workflow(
                    SandboxVerificationWorkflow.run,
                    sandbox_arg,
                    id=f"sandbox-{workflow.info().workflow_id}",
                    task_queue=WORKFLOW_QUEUE,
                    parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
                )
                cancel_wait = asyncio.create_task(
                    workflow.wait_condition(lambda: self.cancel_requested)
                )
                child_cancel_requested = False
                try:
                    await workflow.wait([child, cancel_wait], return_when=asyncio.FIRST_COMPLETED)
                    if self.cancel_requested:
                        child_cancel_requested = True
                        child.cancel()
                        try:
                            await asyncio.shield(child)
                        except ChildWorkflowError as exc:
                            if not isinstance(exc.cause, CancelledError):
                                raise
                        self.phase = "cancelled"
                        return "cancelled"
                    result = await child
                    if pipeline_input is not None and analysis_result is not None:
                        self.pipeline = {
                            "analysis_ref": analysis_result["report"]["ref"],
                            "requirements_ref": analysis_result["requirements"],
                            "sandbox_status": result["status"],
                            "published": result.get("published"),
                        }
                    if result["status"] != "PASSED":
                        if workflow.patched("sandbox-terminal-v1"):
                            # Ticket 14B: one failure terminal state; the cause is a code.
                            self.failure_code = result["status"]
                            self.review_pending = False
                            self.phase = "failed"
                            return "failed"
                        self.phase = result["status"].lower()
                        return self.phase
                    if (
                        pipeline_input is not None
                        and analysis_result is not None
                        and pipeline_input.get("planning")
                        and workflow.patched("planning-handoff-v1")
                    ):
                        # Ticket 15: the passed run becomes the Planning input before planning.
                        refused = await self._hand_off(
                            {**pipeline_input["planning"], **with_ref},
                            analysis_result,
                            result,
                            sandbox_arg,
                        )
                        if refused is not None:
                            self.failure_code = refused
                            self.phase = "failed"
                            return "failed"
                except asyncio.CancelledError:
                    if not child_cancel_requested:
                        child.cancel()
                    try:
                        await asyncio.shield(child)
                    except ChildWorkflowError as exc:
                        if not isinstance(exc.cause, CancelledError):
                            raise
                    raise
                finally:
                    cancel_wait.cancel()
            if name == "planning" and workflow.patched(PLAN_PATCH):
                if self.cancel_requested:
                    self.phase = "cancelled"
                    return "cancelled"
                provisioned = await self._plan_and_provision()
                if provisioned is not None and provisioned != "executed":
                    self.phase = provisioned
                    return provisioned
                if self.cancel_requested:
                    self.phase = "cancelled"
                    return "cancelled"
                continue
            await self._step(name, fn, project_id, queue)
            if self.cancel_requested:
                return "cancelled"

        if provisioned is None:  # scaffold: boolean Signal stands in for approval
            self.phase = "awaiting_approval"
            await workflow.wait_condition(lambda: self.approved or self.cancel_requested)
            if self.cancel_requested:
                self.phase = "cancelled"
                return "cancelled"

        for name, fn, queue in (
            ("provisioning", provision_target, PROVISIONING_QUEUE),
            ("data_copy", copy_data, MIGRATION_QUEUE),
            ("traffic", shift_traffic, MIGRATION_QUEUE),
            ("verification", verify_target, MIGRATION_QUEUE),
        ):
            if name == "provisioning" and provisioned == "executed":
                continue  # the approved provider execution already ran in the approval child
            if (
                name == "data_copy"
                and pipeline_input is not None
                and pipeline_input.get("data_migration")
                and provisioned == "executed"
                and workflow.patched(DATA_PATCH)
            ):
                if self.cancel_requested:
                    self.phase = "cancelled"
                    return "cancelled"
                stopped = await self._data_migration(pipeline_input["data_migration"])
                if stopped:
                    self.phase = stopped
                    return stopped
                continue
            if (
                name == "traffic"
                and provisioned == "executed"
                and workflow.patched(TRAFFIC_HANDOFF_PATCH)
            ):
                # Ticket 50: after a successful execution (and the data step), before the shift.
                if self.cancel_requested:
                    self.phase = "cancelled"
                    return "cancelled"
                stopped = await self._read_traffic_handover()
                if stopped:
                    self.phase = stopped
                    return stopped
                if (
                    pipeline_input is not None
                    and pipeline_input.get("traffic")
                    and workflow.patched(TRAFFIC_SHIFT_PATCH)
                ):
                    # #128: the real shift replaces the scaffold `shift_traffic` step.
                    stopped = await self._shift_traffic(pipeline_input["traffic"])
                    if stopped:
                        self.phase = stopped
                        return stopped
                    continue
            await self._step(name, fn, project_id, queue)
            if self.cancel_requested:
                return "cancelled"

        self.phase = "scaffold_complete"
        return "scaffold_complete"


def _exception_code(exc: BaseException) -> str:
    """F2: a code for an unhandled failure. Exception messages may hold secrets: never kept."""
    if isinstance(exc, ChildWorkflowError):
        return "CHILD_WORKFLOW_FAILED"
    if isinstance(exc, ActivityError):
        return "ACTIVITY_FAILED"
    return "WORKFLOW_FAILED"
