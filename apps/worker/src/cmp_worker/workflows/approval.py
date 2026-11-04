"""Per-Revision approval wait and guarded execution boundary (ticket 07, D4).

The Workflow holds only small identifiers. It never trusts the Signal as approval: the
execution Activity re-reads the business DB approval, latest Revision and Digests first.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import APPROVAL_SIGNAL, APPROVAL_WORKFLOW_NAME, PROVISIONING_QUEUE


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

    @workflow.run
    async def run(self, request: dict) -> dict:
        await workflow.wait_condition(lambda: bool(self.pending))
        self.approval_id = self.pending.pop(0)
        self.state = "APPROVAL_RECEIVED"
        outcome = await workflow.execute_activity(
            "execute_approved_plan",
            self.approval_id,
            task_queue=PROVISIONING_QUEUE,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self.state = outcome["status"]
        self.code = outcome["code"]
        return outcome
