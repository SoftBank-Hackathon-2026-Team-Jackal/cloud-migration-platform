import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import SANDBOX_QUEUE

    from cmp_worker.activities import sandbox


@workflow.defn
class SandboxVerificationWorkflow:
    def __init__(self) -> None:
        self.phase = "created"
        self.attempt = 0
        self.verdict = None
        self.reviewed = False
        self.cleanup_failed = False

    @workflow.query
    def status(self) -> dict:
        return {
            "phase": self.phase,
            "attempt": self.attempt,
            "verdict": self.verdict,
            "cleanup_failed": self.cleanup_failed,
        }

    @workflow.signal
    def review(self) -> None:
        if self.phase == "NEEDS_REVIEW":
            self.reviewed = True

    @workflow.run
    async def run(self, migration_id: str) -> dict:
        async def step(fn, **extra):
            self.phase = fn.__name__
            return await workflow.execute_activity(
                fn,
                {"migration_id": migration_id, "attempt": self.attempt, **extra},
                task_queue=SANDBOX_QUEUE,
                schedule_to_start_timeout=timedelta(minutes=5),
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

        outcome = "FAILED"
        infra_failures = 0
        sandbox_prepared = False
        failed = False
        try:
            self.attempt = 1
            await step(sandbox.request_build)
            sandbox_prepared = True
            await step(sandbox.prepare_sandbox)
            while True:
                await step(sandbox.render_sandbox)
                await step(sandbox.apply_sandbox)
                await step(sandbox.run_sandbox_tests)
                self.verdict = await step(sandbox.judge_sandbox)
                if self.verdict["passed"]:
                    await step(sandbox.publish_final_report)
                    outcome = "PASSED"
                    break
                failure = await step(
                    sandbox.classify_sandbox_failure,
                    failure_class=self.verdict["failure_class"],
                )
                if failure == "RENDER":
                    outcome = "STOPPED_RENDER"
                    break
                if failure == "INFRA":
                    infra_failures += 1
                    if infra_failures >= 2:
                        outcome = "NEEDS_REVIEW"
                        break
                    continue
                infra_failures = 0
                if failure not in ("SPEC", "CODE", "BUILD"):
                    raise ApplicationError(
                        "Unknown sandbox failure class",
                        type="UnknownFailureClass",
                        non_retryable=True,
                    )
                if self.attempt >= 3:
                    outcome = "NEEDS_REVIEW"
                    break
                await step(sandbox.fix_sandbox, failure_class=failure)
                self.attempt += 1
                if failure in ("CODE", "BUILD"):
                    await step(sandbox.request_build)
        except BaseException:
            failed = True
            raise
        finally:
            # Nothing to clean up if the run failed before the sandbox was prepared.
            # A cleanup failure must not hide the error that is already propagating.
            if sandbox_prepared:
                try:
                    await asyncio.shield(step(sandbox.teardown_sandbox))
                except ActivityError:
                    self.cleanup_failed = True
                    if not failed:
                        raise
        if outcome == "NEEDS_REVIEW":
            self.phase = outcome
            await workflow.wait_condition(lambda: self.reviewed)
        else:
            self.phase = outcome
        return {"status": outcome, "mode": "scaffold"}
