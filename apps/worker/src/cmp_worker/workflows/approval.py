"""Per-Revision approval wait and guarded provider execution (tickets 07/08).

The Workflow holds only small identifiers. It never trusts the Signal as approval: the claim
Activity re-reads the business DB approval, latest Revision and Digests and writes a unique
RUNNING marker. Provider Activities run once (no retry); a lost Activity is recorded UNKNOWN and
needs state inspection, never automatic re-execution.

Cancellation (ticket 14, F3) never means deletion: before the claim it ends without a marker;
after it, the claim/complete DB Activities are shielded so a RUNNING marker is always finished,
and a cancelled provider step is recorded UNKNOWN/EXECUTION_CANCELLED.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.execution import execution_steps
    from cmp_contracts.queues import APPROVAL_SIGNAL, APPROVAL_WORKFLOW_NAME, PROVISIONING_QUEUE

DB_ACTIVITY = {
    "task_queue": PROVISIONING_QUEUE,
    "start_to_close_timeout": timedelta(minutes=2),
    "retry_policy": RetryPolicy(maximum_attempts=3),
}
PROVIDER_ACTIVITY = {
    "task_queue": PROVISIONING_QUEUE,
    "start_to_close_timeout": timedelta(hours=2),
    "heartbeat_timeout": timedelta(minutes=5),
    "retry_policy": RetryPolicy(maximum_attempts=1),
}


@workflow.defn(name=APPROVAL_WORKFLOW_NAME)
class ProvisioningApprovalWorkflow:
    def __init__(self) -> None:
        self.state = "AWAITING_APPROVAL"
        self.code: str | None = None
        self.approval_id: str | None = None
        self.pending: list[str] = []
        self.seen: set[str] = set()

    @workflow.signal(name=APPROVAL_SIGNAL)
    def approval_recorded(self, approval_id: str) -> None:
        if approval_id not in self.seen:  # Duplicate re-delivery is a no-op.
            self.seen.add(approval_id)
            self.pending.append(approval_id)

    @workflow.query
    def status(self) -> dict[str, object]:
        return {"state": self.state, "approval_id": self.approval_id, "code": self.code}

    def _finish(self, outcome: dict) -> dict:
        self.state = outcome["status"]
        self.code = outcome["code"]
        return outcome

    async def _shielded(self, name: str, arg) -> tuple[dict, bool]:
        """Run a DB Activity to completion even if the Workflow is cancelled meanwhile."""
        task = asyncio.ensure_future(workflow.execute_activity(name, arg, **DB_ACTIVITY))
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                cancelled = True

    @workflow.run
    async def run(self, request: dict) -> dict:
        try:
            await workflow.wait_condition(lambda: bool(self.pending))
        except asyncio.CancelledError:
            return self._finish({"status": "CANCELLED", "code": "CANCELLED_BEFORE_EXECUTION"})
        self.approval_id = self.pending.pop(0)
        self.state = "APPROVAL_RECEIVED"
        claim, cancelled = await self._shielded("claim_approved_execution", self.approval_id)
        if claim["status"] != "CLAIMED":
            return self._finish(claim)

        steps = execution_steps(claim["provider"], claim["scope"])
        status, code, refs, step = "SUCCEEDED", "EXECUTION_SUCCEEDED", {}, None
        if cancelled:
            # Claimed but no provider step started: nothing changed on the target.
            steps, status, code = (), "FAILED", "CANCELLED_BEFORE_EXECUTION"
        elif not steps:
            status, code = "FAILED", "NO_EXECUTION_PATH"
        for name in steps:
            self.state = "EXECUTING"
            step = name  # recorded so the result can tell infrastructure from workload failures
            try:
                # Later steps (e.g. FULL workloads) receive refs produced by earlier steps.
                request = {**claim, "resource_refs": dict(refs)}
                result = await workflow.execute_activity(name, request, **PROVIDER_ACTIVITY)
            except ActivityError as error:
                lost = "EXECUTION_CANCELLED" if is_cancelled_exception(error) else None
                result = {"status": "UNKNOWN", "code": lost or "PROVIDER_ACTIVITY_LOST"}
            except asyncio.CancelledError:
                # The step may still be changing the target: unknown, never "deleted" (F3).
                result = {"status": "UNKNOWN", "code": "EXECUTION_CANCELLED"}
            refs.update(result.get("resource_refs", {}))
            if result["status"] != "SUCCEEDED":
                status, code = result["status"], result["code"]
                break
            code = result["code"]

        await self._shielded(
            "complete_approved_execution",
            {
                "approval_id": self.approval_id,
                "status": status,
                "code": code,
                "resource_refs": refs,
                "step": step,
            },
        )
        final = "EXECUTED" if status == "SUCCEEDED" else status
        return self._finish(
            {
                "approval_id": self.approval_id,
                "plan_id": claim["plan_id"],
                "revision": claim["revision"],
                "status": final,
                "code": code,
            }
        )
