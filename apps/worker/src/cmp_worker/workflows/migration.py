from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import (
        ANALYSIS_QUEUE,
        BUILD_QUEUE,
        MIGRATION_QUEUE,
        PROVISIONING_QUEUE,
    )

    from cmp_worker.activities.analysis import analyze_project
    from cmp_worker.activities.build import prepare_image
    from cmp_worker.activities.migration import copy_data, shift_traffic, verify_target
    from cmp_worker.activities.provisioning import prepare_plan, provision_target


@workflow.defn
class MigrationWorkflow:
    def __init__(self) -> None:
        self.phase = "created"
        self.approved = False
        self.cancel_requested = False

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
        }

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
        for name, fn, queue in (
            ("analysis", analyze_project, ANALYSIS_QUEUE),
            ("build", prepare_image, BUILD_QUEUE),
            ("planning", prepare_plan, PROVISIONING_QUEUE),
        ):
            await self._step(name, fn, project_id, queue)
            if self.cancel_requested:
                return "cancelled"

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
            await self._step(name, fn, project_id, queue)
            if self.cancel_requested:
                return "cancelled"

        self.phase = "scaffold_complete"
        return "scaffold_complete"
