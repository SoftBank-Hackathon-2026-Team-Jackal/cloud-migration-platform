"""Ticket 32: a periodic sweep of Sandbox Namespaces that are past their expiry label.

A finished Sandbox run asks the cluster to delete its Namespace without waiting for it. This
Schedule is what removes one that never went away (the 2-hour expiry label alone deletes nothing).
The Activity runs on the Sandbox queue, where the cluster is; with no such worker the run times out
waiting to start and the next one tries again.
"""

from datetime import timedelta

from cmp_contracts.queues import SANDBOX_QUEUE, WORKFLOW_QUEUE
from temporalio import workflow
from temporalio.common import RetryPolicy

SWEEP_ACTIVITY = "sbx_cleanup_expired"
SCHEDULE_ID = "cmp-sandbox-expire-sweep"
SWEEP_INTERVAL = timedelta(minutes=15)


@workflow.defn(name="SandboxExpireWorkflow")
class SandboxExpireWorkflow:
    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            SWEEP_ACTIVITY,
            {},
            task_queue=SANDBOX_QUEUE,
            schedule_to_start_timeout=timedelta(minutes=5),
            start_to_close_timeout=timedelta(minutes=20),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


async def ensure_sandbox_expire_schedule(client) -> bool:
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
                    SandboxExpireWorkflow.run,
                    id=f"{SCHEDULE_ID}-run",
                    task_queue=WORKFLOW_QUEUE,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=SWEEP_INTERVAL)]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
    except ScheduleAlreadyRunningError:
        return False
    except Exception:  # noqa: BLE001 - startup must not fail; retried at the next start
        import logging

        logging.getLogger(SCHEDULE_ID).warning("SANDBOX_EXPIRE_SCHEDULE_UNAVAILABLE")
        return False
    return True
