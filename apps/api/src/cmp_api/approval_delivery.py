"""Deliver a recorded approval to its per-Revision Temporal Workflow (Signal-With-Start).

Delivery is retry-safe: the Workflow ignores an approval ID it already handled, and a closed
Workflow for the same Revision is treated as already processed. Delivery never means execution.
"""

from cmp_contracts.queues import (
    APPROVAL_SIGNAL,
    APPROVAL_WORKFLOW_NAME,
    WORKFLOW_QUEUE,
    approval_workflow_id,
)
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from cmp_api.temporal_client import get_temporal_client


class TemporalApprovalDelivery:
    def __init__(self, client: Client | None = None):
        self._client = client

    async def _get_client(self) -> Client:
        return self._client or await get_temporal_client()

    async def deliver(self, *, approval_id: str, plan_id: str, revision: int) -> None:
        client = await self._get_client()
        try:
            await client.start_workflow(
                APPROVAL_WORKFLOW_NAME,
                {"plan_id": plan_id, "revision": revision},
                id=approval_workflow_id(plan_id, revision),
                task_queue=WORKFLOW_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                start_signal=APPROVAL_SIGNAL,
                start_signal_args=[approval_id],
            )
        except WorkflowAlreadyStartedError:
            return  # A completed run already handled this Revision; never start a second one.

    async def workflow_state(self, *, plan_id: str, revision: int) -> str:
        try:
            client = await self._get_client()
            handle = client.get_workflow_handle(approval_workflow_id(plan_id, revision))
            state = await handle.query("status")
        except RPCError as exc:
            return "NOT_STARTED" if exc.status == RPCStatusCode.NOT_FOUND else "UNAVAILABLE"
        except Exception:  # noqa: BLE001 - any query failure is reported, not raised
            return "UNAVAILABLE"
        return str(state["state"])
