"""Worker for the Sandbox activity queue."""

from cmp_contracts.queues import SANDBOX_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities import sandbox
from cmp_worker.activities.sandbox_loop import SandboxLoopActivities


def create_worker(client: Client, loop: SandboxLoopActivities | None = None) -> Worker:
    """`loop` carries the real Sandbox loop Activities (ticket 14B). It is injected because its
    build step (D-12) and model client (ADR-004) are not configured yet."""
    return Worker(
        client,
        task_queue=SANDBOX_QUEUE,
        activities=[
            *(loop.all() if loop is not None else []),
            sandbox.sandbox_queue_probe,
            sandbox.request_build,
            sandbox.prepare_sandbox,
            sandbox.render_sandbox,
            sandbox.apply_sandbox,
            sandbox.run_sandbox_tests,
            sandbox.judge_sandbox,
            sandbox.classify_sandbox_failure,
            sandbox.fix_sandbox,
            sandbox.publish_final_report,
            sandbox.teardown_sandbox,
        ],
    )
