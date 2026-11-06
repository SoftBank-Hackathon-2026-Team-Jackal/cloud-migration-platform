"""Ticket 48 (#183) G7: one-Activity Workflow behind the API's GCP connection check."""

from datetime import timedelta

from cmp_contracts.queues import (
    GCP_CONNECTION_CHECK_ACTIVITY,
    GCP_CONNECTION_CHECK_WORKFLOW_NAME,
    PROVISIONING_QUEUE,
)
from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name=GCP_CONNECTION_CHECK_WORKFLOW_NAME)
class GcpConnectionCheckWorkflow:
    @workflow.run
    async def run(self, expected_principal: str) -> dict:
        return await workflow.execute_activity(
            GCP_CONNECTION_CHECK_ACTIVITY,
            expected_principal,
            task_queue=PROVISIONING_QUEUE,
            # print-access-token (60 s cap) + tokeninfo (15 s cap) normally take ~1-2 s.
            start_to_close_timeout=timedelta(seconds=20),
            schedule_to_close_timeout=timedelta(seconds=40),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
