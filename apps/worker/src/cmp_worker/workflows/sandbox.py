import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import SANDBOX_QUEUE

    from cmp_worker.activities import sandbox
    from cmp_worker.sandbox_deploy import sandbox_namespace


@workflow.defn
class SandboxVerificationWorkflow:
    def __init__(self) -> None:
        self.phase = "created"
        self.attempt = 0
        self.verdict = None
        self.reviewed = False
        self.cleanup_failed = False
        self.attempts: list[dict] = []
        self.integrated = False

    @workflow.query
    def status(self) -> dict:
        return {
            "phase": self.phase,
            "attempt": self.attempt,
            "verdict": self.verdict,
            "cleanup_failed": self.cleanup_failed,
            "attempts": self.attempts,
        }

    @workflow.signal
    def review(self) -> None:
        if self.phase == "NEEDS_REVIEW":
            self.reviewed = True

    @workflow.signal
    def resolve_review(self) -> None:
        """A person has looked at a stopped run (integrated path). Not an approval."""
        if self.phase == "NEEDS_REVIEW":
            self.reviewed = True

    @workflow.run
    async def run(self, data: str | dict) -> dict:
        # A string is the scaffold input (migration ID only); a dict is a real `SandboxInput`.
        if isinstance(data, str):
            return await self._run_scaffold(data)
        self.integrated = True
        return await self._run_integrated(data)

    async def _run_scaffold(self, migration_id: str) -> dict:
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

    # ------------------------------------------------------------------ integrated

    async def _tell_parent(self, pending: bool) -> None:
        parent = workflow.info().parent
        if parent is None:
            return
        try:
            handle = workflow.get_external_workflow_handle(parent.workflow_id)
            await handle.signal("sandbox_review_pending", pending)
        except Exception:  # noqa: BLE001 - a missing parent must not change the outcome
            workflow.logger.info("parent could not be told about the review state")

    async def _run_integrated(self, data: dict) -> dict:
        async def act(name, payload, *, minutes=3):
            return await workflow.execute_activity(
                name,
                payload,
                task_queue=SANDBOX_QUEUE,
                schedule_to_start_timeout=timedelta(minutes=5),
                start_to_close_timeout=timedelta(minutes=minutes),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

        namespace = sandbox_namespace(data["migration_id"], data["run_id"])
        requirements_ref, source = data["requirements_ref"], data["source"]
        outcome, reasons = "FAILED", []
        prepared = False
        failed = False
        try:
            self.phase = "sbx_build"
            images = (
                await act(
                    "sbx_build",
                    {"requirements_ref": requirements_ref, "source": source},
                    minutes=30,
                )
            )["images"]
            self.phase = "sbx_prepare"
            prepared = True
            prep = await act(
                "sbx_prepare",
                {"input": data, "requirements_ref": requirements_ref, "images": images},
            )
            plan, previous = prep["plan"], None
            if plan is None:
                reasons = [f"SANDBOX:{prep['sandbox_error']}"]
                outcome = "NEEDS_REVIEW"
            infra_streak, tests_ran, baseline_ok = 0, False, False
            self.attempt = 1
            while plan is not None:
                self.phase = "sbx_apply"
                applied = await act(
                    "sbx_apply",
                    {
                        "input": data,
                        "plan": plan,
                        "previous_plan": previous,
                        "attempt": self.attempt,
                        "baseline_ok": baseline_ok,
                    },
                    minutes=20,
                )
                baseline_ok = baseline_ok or applied.get("baseline") in ("SAVED", "KEPT")
                self.phase = "sbx_verify"
                verified = await act(
                    "sbx_verify",
                    {
                        "input": data,
                        "plan": plan,
                        "applied": applied,
                        "source": source,
                        "suite_ref": data["suite_ref"],
                        "reset": tests_ran,
                    },
                    minutes=15,
                )
                tests_ran = tests_ran or applied.get("sandbox_error") is None
                self.verdict = verified["verdict"]
                if self.verdict["passed"]:
                    self.attempts.append({"attempt": self.attempt, "passed": True})
                    outcome = "PASSED"
                    break
                self.phase = "sbx_classify"
                classified = await act(
                    "sbx_classify",
                    {"plan": plan, "requirements_ref": requirements_ref, "verified": verified},
                )
                klass = classified["failure_class"]
                self.attempts.append(
                    {
                        "attempt": self.attempt,
                        "passed": False,
                        "failure_class": klass,
                        "reasons": classified["reasons"],
                    }
                )
                reasons = classified["reasons"]
                if klass == "RENDER":
                    outcome = "STOPPED_RENDER"
                    break
                if klass == "INFRA":
                    infra_streak += 1
                    if infra_streak >= 2:
                        outcome = "NEEDS_REVIEW"
                        break
                    continue
                infra_streak = 0
                if self.attempt >= 3:
                    outcome = "NEEDS_REVIEW"
                    break
                self.phase = "sbx_fix"
                fix = await act(
                    "sbx_fix",
                    {
                        "classification": classified,
                        "requirements_ref": requirements_ref,
                        "logs_ref": verified["logs_ref"],
                        "suite_ref": data["suite_ref"],
                        "source": source,
                    },
                    minutes=20,
                )
                if fix["status"] != "PROPOSED":
                    reasons = [*reasons, f"FIXER:{fix.get('reason') or fix['status']}"]
                    outcome = "NEEDS_REVIEW"
                    break
                self.attempt += 1
                if fix["kind"] == "REQUIREMENTS":
                    requirements_ref = fix["requirements_ref"]
                else:
                    source = fix["source"]
                    self.phase = "sbx_build"
                    images = (
                        await act(
                            "sbx_build",
                            {"requirements_ref": requirements_ref, "source": source},
                            minutes=30,
                        )
                    )["images"]
                prep = await act(
                    "sbx_prepare",
                    {
                        "input": data,
                        "requirements_ref": requirements_ref,
                        "images": images,
                        "previous_plan": plan,
                    },
                )
                if prep["plan"] is None:
                    reasons = [f"SANDBOX:{prep['sandbox_error']}"]
                    outcome = "NEEDS_REVIEW"
                    break
                previous, plan = plan, prep["plan"]
        except BaseException:
            failed = True
            raise
        finally:
            if prepared:
                try:
                    await asyncio.shield(act("sbx_teardown", {"namespace": namespace}, minutes=10))
                except ActivityError:
                    self.cleanup_failed = True
                    if not failed:
                        raise
        self.phase = outcome
        if outcome == "NEEDS_REVIEW":
            await self._tell_parent(True)
            await workflow.wait_condition(lambda: self.reviewed)
            await self._tell_parent(False)
        return {
            "status": outcome,
            "mode": "integrated",
            "attempts": self.attempts,
            "reasons": reasons,
            "requirements_ref": requirements_ref,
            "source": source,
        }
