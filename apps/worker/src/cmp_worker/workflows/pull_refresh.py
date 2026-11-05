"""Ticket 29 (I10): platform-side periodic refresh of GKE ECR pull Secrets.

A Temporal Schedule starts this Workflow every 6 hours (half the 12-hour ECR token TTL). It only
calls the provisioning Activity, which re-checks approvals and re-applies the pull Secret; the
token stays inside that Activity. A CronJob in the cluster is not used because it would need
AWS credentials there.
"""

from datetime import timedelta

from cmp_contracts.queues import PROVISIONING_QUEUE, WORKFLOW_QUEUE
from temporalio import workflow
from temporalio.common import RetryPolicy

REFRESH_ACTIVITY = "refresh_gcp_pull_secrets"
SCHEDULE_ID = "cmp-gcp-ecr-pull-refresh"
REFRESH_INTERVAL = timedelta(hours=6)


@workflow.defn(name="GcpPullSecretRefreshWorkflow")
class GcpPullSecretRefreshWorkflow:
    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            REFRESH_ACTIVITY,
            {},
            task_queue=PROVISIONING_QUEUE,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )


async def ensure_refresh_schedule(client) -> bool:
    """Create the Schedule once; an existing one is kept. Returns True when created.
    Any other failure is logged as a code and does not stop the workers from starting."""
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
    )

    try:
        await client.create_schedule(
            SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    GcpPullSecretRefreshWorkflow.run,
                    id=f"{SCHEDULE_ID}-run",
                    task_queue=WORKFLOW_QUEUE,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=REFRESH_INTERVAL)]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
    except ScheduleAlreadyRunningError:
        return False
    except Exception:  # noqa: BLE001 - startup must not fail; retried at the next start
        import logging

        logging.getLogger(SCHEDULE_ID).warning("PULL_REFRESH_SCHEDULE_UNAVAILABLE")
        return False
    return True
