import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import CancelledError, ChildWorkflowError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.plan_publish import DEFAULT_APPROVAL_TIMEOUT_SECONDS, PLAN_PATCH
    from cmp_contracts.queues import (
        ANALYSIS_QUEUE,
        APPROVAL_WORKFLOW_NAME,
        BUILD_QUEUE,
        MIGRATION_QUEUE,
        PROVISIONING_QUEUE,
        WORKFLOW_QUEUE,
        approval_workflow_id,
    )

    from cmp_worker.activities.analysis import analyze_project
    from cmp_worker.activities.build import prepare_image
    from cmp_worker.activities.migration import copy_data, shift_traffic, verify_target
    from cmp_worker.activities.provisioning import prepare_plan, provision_target
    from cmp_worker.workflows.analysis import AnalysisWorkflow
    from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow


@workflow.defn
class MigrationWorkflow:
    def __init__(self) -> None:
        self.phase = "created"
        self.approved = False
        self.cancel_requested = False
        self.plan: dict | None = None
        self.plan_code: str | None = None
        self.review_pending = False
        self.failure_code: str | None = None

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
            "review_pending": self.review_pending,  # ticket 14B: in progress, not terminal
            "failure_code": self.failure_code,
        }

    async def _plan_and_provision(self) -> str | None:
        """Ticket 18 P7: publish the Plan, wait for its Revision approval child, return the outcome.

        None means planning is not configured (scaffold path). Otherwise a terminal phase, or
        "executed" when the approved provider execution succeeded.
        """
        self.phase = "planning"
        published = await workflow.execute_activity(
            "publish_migration_plan",
            {"migration_id": workflow.info().workflow_id},
            task_queue=PROVISIONING_QUEUE,
            start_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),  # publication is input-idempotent
        )
        if published["status"] == "SCAFFOLD":
            return None
        self.plan_code = published["code"]
        if published["status"] != "PUBLISHED":
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
    async def run(self, project_id: str) -> str:
        if workflow.patched("analysis-child-v1"):
            if self.cancel_requested:
                self.phase = "cancelled"
                return "cancelled"
            self.phase = "analysis"
            migration_id = workflow.info().workflow_id
            child = await workflow.start_child_workflow(
                AnalysisWorkflow.run,
                migration_id,
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
                await child
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
                child = await workflow.start_child_workflow(
                    SandboxVerificationWorkflow.run,
                    workflow.info().workflow_id,
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
                    if result["status"] != "PASSED":
                        if workflow.patched("sandbox-terminal-v1"):
                            # Ticket 14B: one failure terminal state; the cause is a code.
                            self.failure_code = result["status"]
                            self.review_pending = False
                            self.phase = "failed"
                            return "failed"
                        self.phase = result["status"].lower()
                        return self.phase
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
            await self._step(name, fn, project_id, queue)
            if self.cancel_requested:
                return "cancelled"

        self.phase = "scaffold_complete"
        return "scaffold_complete"
