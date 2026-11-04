"""Per-Revision approval wait and guarded provider execution (tickets 07/08).

The Workflow holds only small identifiers. It never trusts the Signal as approval: the claim
Activity re-reads the business DB approval, latest Revision and Digests and writes a unique
RUNNING marker. Provider Activities run once (no retry); a lost Activity is recorded UNKNOWN and
needs state inspection, never automatic re-execution.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

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

    @workflow.run
    async def run(self, request: dict) -> dict:
        await workflow.wait_condition(lambda: bool(self.pending))
        self.approval_id = self.pending.pop(0)
        self.state = "APPROVAL_RECEIVED"
        claim = await workflow.execute_activity(
            "claim_approved_execution", self.approval_id, **DB_ACTIVITY
        )
        if claim["status"] != "CLAIMED":
            return self._finish(claim)

        steps = execution_steps(claim["provider"], claim["scope"])
        status, code, refs = "SUCCEEDED", "EXECUTION_SUCCEEDED", {}
        if not steps:
            status, code = "FAILED", "NO_EXECUTION_PATH"
        for name in steps:
            self.state = "EXECUTING"
            try:
                result = await workflow.execute_activity(name, claim, **PROVIDER_ACTIVITY)
            except ActivityError:
                result = {"status": "UNKNOWN", "code": "PROVIDER_ACTIVITY_LOST"}
            refs.update(result.get("resource_refs", {}))
            if result["status"] != "SUCCEEDED":
                status, code = result["status"], result["code"]
                break
            code = result["code"]

        await workflow.execute_activity(
            "complete_approved_execution",
            {
                "approval_id": self.approval_id,
                "status": status,
                "code": code,
                "resource_refs": refs,
            },
            **DB_ACTIVITY,
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
