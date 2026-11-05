import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import SANDBOX_QUEUE

    from cmp_worker.activities import sandbox
    from cmp_worker.review_gate import ReapprovalGate
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
        self.reapproval: ReapprovalGate | None = None
        self.early_rejections: list[dict[str, str]] = []
        self.ai_tests: dict | None = None

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
    def approve_changed_rows(self, payload: dict) -> None:
        """A person answers for rows whose handling the loop changed (ticket 14C)."""
        if self.reapproval is None:
            self.early_rejections.append(
                {"signal": "approve_losses", "code": "NOT_WAITING", "detail": ""}
            )
            return
        self.reapproval.approve(payload)

    @workflow.query
    def reapproval_state(self) -> dict:
        if self.reapproval is None:
            return {"phase": "NONE", "rejections": list(self.early_rejections)}
        view = self.reapproval.view()
        view["rejections"] = [*self.early_rejections, *view["rejections"]]
        return view

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
        row_changes: dict[str, str] = {}
        fix_reasons: list[str] = []
        last_results: list[dict] = []
        built_sha: str | None = None
        builds_ref: dict | None = None
        suite_ref = data["suite_ref"]
        try:
            self.phase = "sbx_build"
            built = await act(
                "sbx_build",
                {
                    "requirements_ref": requirements_ref,
                    "source": source,
                    "migration_id": data["migration_id"],
                    "run_id": data["run_id"],
                    "attempt": 1,
                },
                minutes=30,
            )
            builds_ref = built.get("builds_ref")
            plan, previous = None, None
            if built.get("build_error"):
                # The real build path produced no image: a person looks at it, no Sandbox exists.
                reasons = [f"BUILD:{built['build_error']}"]
                outcome = "NEEDS_REVIEW"
            else:
                images, built_sha = built["images"], built.get("source_sha256")
                self.phase = "sbx_prepare"
                prepared = True
                prep = await act(
                    "sbx_prepare",
                    {"input": data, "requirements_ref": requirements_ref, "images": images},
                )
                plan = prep["plan"]
                if plan is None:
                    reasons = [f"SANDBOX:{prep['sandbox_error']}"]
                    outcome = "NEEDS_REVIEW"
            infra_streak, tests_ran, baseline_ok = 0, False, False
            ai_pending = True
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
                # AI tests are asked for once, on the first healthy Sandbox, and only when switched
                # on. Old histories never reach `patched`, so they replay as before.
                if (
                    ai_pending
                    and applied.get("sandbox_error") is None
                    and data.get("ai_tests")
                    and workflow.patched("ai-tests-v1")
                ):
                    self.phase = "sbx_ai_tests"
                    asked = await act(
                        "sbx_ai_tests",
                        {
                            "plan": plan,
                            "input": data,
                            "requirements_ref": requirements_ref,
                            "suite_ref": suite_ref,
                            "analysis_report_ref": data.get("analysis_report_ref"),
                            "migration_id": data["migration_id"],
                        },
                        minutes=20,
                    )
                    suite_ref = asked["suite_ref"]
                    self.ai_tests = {
                        k: asked.get(k) for k in ("status", "reason", "accepted", "rejected")
                    }
                if applied.get("sandbox_error") is None:
                    ai_pending = False
                self.phase = "sbx_verify"
                verified = await act(
                    "sbx_verify",
                    {
                        "input": data,
                        "plan": plan,
                        "applied": applied,
                        "source": source,
                        "suite_ref": suite_ref,
                        "reset": tests_ran,
                    },
                    minutes=15,
                )
                tests_ran = tests_ran or applied.get("sandbox_error") is None
                self.verdict = verified["verdict"]
                if self.verdict["passed"]:
                    last_results = verified["results"]
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
                        "suite_ref": suite_ref,
                        "source": source,
                        "analysis_report_ref": data.get("analysis_report_ref"),
                        "migration_id": data["migration_id"],
                    },
                    minutes=20,
                )
                if fix["status"] != "PROPOSED":
                    reasons = [*reasons, f"FIXER:{fix.get('reason') or fix['status']}"]
                    outcome = "NEEDS_REVIEW"
                    break
                self.attempt += 1
                fix_reasons.append(fix.get("reason") or "")
                if fix["kind"] == "REQUIREMENTS":
                    requirements_ref = fix["requirements_ref"]
                    row_changes.update(fix.get("row_changes") or {})
                else:
                    source = fix["source"]
                    self.phase = "sbx_build"
                    rebuilt = await act(
                        "sbx_build",
                        {
                            "requirements_ref": requirements_ref,
                            "source": source,
                            "migration_id": data["migration_id"],
                            "run_id": data["run_id"],
                            "attempt": self.attempt,
                        },
                        minutes=30,
                    )
                    if rebuilt.get("build_error"):
                        reasons = [f"BUILD:{rebuilt['build_error']}"]
                        outcome = "NEEDS_REVIEW"
                        break
                    images, built_sha = rebuilt["images"], rebuilt.get("source_sha256")
                    builds_ref = rebuilt.get("builds_ref") or builds_ref
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
        published = None
        if outcome == "PASSED":
            self.phase = "sbx_plan_publish"
            publish = {
                "input": {**data, "suite_ref": suite_ref},
                "requirements_ref": requirements_ref,
                "source": source,
                "row_changes": row_changes,
                "results": last_results,
            }
            planned = await act("sbx_plan_publish", publish)
            decisions = None
            if planned["error"]:
                outcome, reasons = "NEEDS_REVIEW", [f"PUBLISH:{planned['error']}"]
            elif planned["changed"]:
                # Rows the loop handled differently go back to a person before anything is published.
                self.reapproval = ReapprovalGate(
                    planned["final_ref"],
                    planned["changed"],
                    required=planned["required"],
                    actor=planned["actor"],
                )
                self.phase = "REAPPROVAL_PENDING"
                await self._tell_parent(True)
                await workflow.wait_condition(lambda: self.reapproval.finished)
                await self._tell_parent(False)
                if self.reapproval.phase == "BLOCKED":
                    outcome = "NEEDS_REVIEW"
                    reasons = [f"ROW_BLOCKED:{b['id']}" for b in self.reapproval.blocked()]
                else:
                    decisions = self.reapproval.decisions()
            if outcome == "PASSED":
                self.phase = "sbx_publish"
                published = await act(
                    "sbx_publish",
                    {
                        **publish,
                        "decisions": decisions,
                        "built_source_sha256": built_sha,
                        "fix_reasons": [r for r in fix_reasons if r],
                    },
                    minutes=5,
                )
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
            "published": published,
            **({"ai_tests": self.ai_tests} if self.ai_tests is not None else {}),
            **({"builds_ref": builds_ref} if builds_ref is not None else {}),
            **({"suite_ref": suite_ref} if suite_ref != data["suite_ref"] else {}),
        }
