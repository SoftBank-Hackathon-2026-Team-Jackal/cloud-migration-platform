import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import SANDBOX_QUEUE

    from cmp_worker.activities import sandbox
    from cmp_worker.redteam_stage import MAX_SECURITY_ATTEMPTS
    from cmp_worker.review_gate import ReapprovalGate
    from cmp_worker.sandbox_deploy import sandbox_namespace
    from cmp_worker.sandbox_events import attempt_data, context, event_key, image_digest

# Provisioning 59: the loop emits `build.*`/`sandbox.*`. Histories without this marker replay as
# before (no events context, no `sbx_emit`).
SANDBOX_EVENTS_PATCH = "sandbox-events-v1"
# Ticket 37: the red-team step. Only a run whose input asks for it ever reaches this marker, so
# histories that never had the field replay exactly as before.
REDTEAM_PATCH = "redteam-v1"


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
        # Ticket 37: the red-team step's waiting room and last report.
        self.redteam_phase = "NONE"
        self.redteam_round = 0
        self.redteam_request: dict | None = None
        self.redteam_approval: dict | None = None
        self.redteam_declined = False
        self.redteam_rejections: list[dict[str, str]] = []

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
    def approve_redteam(self, approval: dict) -> None:
        """A person says yes to one attack on this Sandbox copy (R8). Each run needs its own."""
        if self.redteam_phase != "WAITING_FOR_APPROVAL":
            self.redteam_rejections.append({"signal": "approve_redteam", "code": "NOT_WAITING"})
            return
        self.redteam_approval = approval

    @workflow.signal
    def decline_redteam(self) -> None:
        if self.redteam_phase != "WAITING_FOR_APPROVAL":
            self.redteam_rejections.append({"signal": "decline_redteam", "code": "NOT_WAITING"})
            return
        self.redteam_declined = True

    @workflow.query
    def redteam_state(self) -> dict:
        waiting = self.redteam_phase == "WAITING_FOR_APPROVAL"
        return {
            "phase": self.redteam_phase,
            "waiting_since": self.redteam_round if waiting else 0,
            "request": self.redteam_request if waiting else None,
            "rejections": list(self.redteam_rejections),
        }

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
        async def act(name, payload, *, minutes=3, attempts=2):
            return await workflow.execute_activity(
                name,
                payload,
                task_queue=SANDBOX_QUEUE,
                schedule_to_start_timeout=timedelta(minutes=5),
                start_to_close_timeout=timedelta(minutes=minutes),
                retry_policy=RetryPolicy(maximum_attempts=attempts),
            )

        # Event plumbing (Provisioning 59). `run` counts `sbx_apply` calls (the screen's tracks);
        # `self.attempt` is the fix attempt. Emitting is observation: it never changes the loop.
        events_on = workflow.patched(SANDBOX_EVENTS_PATCH)
        mid = data["migration_id"]
        run = 1

        def ctx(run_no: int, attempt: int) -> dict:
            return {"events": context(mid, run_no, attempt)} if events_on else {}

        async def emit(*items: tuple[str, str, dict, str]) -> None:
            """Workflow-level events through `sbx_emit`; items are (type, status, data, key)."""
            if not events_on:
                return
            try:
                await workflow.execute_activity(
                    "sbx_emit",
                    {
                        "migration_id": mid,
                        "events": [
                            {"type": t, "status": st, "data": d, "key": k} for t, st, d, k in items
                        ],
                    },
                    task_queue=SANDBOX_QUEUE,
                    schedule_to_start_timeout=timedelta(minutes=1),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                workflow.logger.info("sandbox events could not be stored")

        async def emit_attempt(
            run_no, attempt, verdict, klass, checks, fix_ids=None, state="judged"
        ):
            data = attempt_data(
                run_no,
                attempt,
                verdict=verdict,
                failure_class=klass,
                checks=checks,
                fix_ids=fix_ids,
            )
            await emit(
                ("sandbox.attempt", "running", data, event_key(run_no, attempt, "attempt", state))
            )

        async def emit_done(cleanup: str, digest: str | None) -> None:
            status = (
                "running" if cleanup == "PENDING" else ("done" if outcome == "PASSED" else "failed")
            )
            data = {"status": outcome, "cleanup_status": cleanup, "digest": digest}
            await emit(
                ("sandbox.done", status, data, event_key(run, self.attempt, "done", cleanup))
            )

        red_on = bool(data.get("redteam")) and workflow.patched(REDTEAM_PATCH)
        red_open: list[dict] = []  # Findings still open on the last image (with their requests)
        red_result: dict | None = None
        sec_attempts = 0

        def red_report(status: str, code: str | None, open_: list[dict], **more) -> dict:
            return {"status": status, "code": code, "open": open_, **more}

        async def redteam_round(plan, digest, previous: list[dict]) -> dict:
            """Ask a person, wait for the answer inside the Sandbox's life, then run once. A
            refusal of any kind is a report, not a failure: the run has already passed (R3)."""
            self.redteam_round += 1
            asked = await act("sbx_redteam_request", {"input": data, "plan": plan})
            if asked["wait_seconds"] <= 0:
                return {"status": "SKIPPED", "code": "REDTEAM_TIME_INSUFFICIENT", "open": previous}
            self.redteam_request, self.redteam_approval = asked["request"], None
            self.redteam_declined = False
            self.redteam_phase, self.phase = "WAITING_FOR_APPROVAL", "REDTEAM_APPROVAL_PENDING"
            await self._tell_parent(True)
            try:
                await workflow.wait_condition(
                    lambda: self.redteam_approval is not None or self.redteam_declined,
                    timeout=timedelta(seconds=asked["wait_seconds"]),
                )
            except TimeoutError:
                pass
            await self._tell_parent(False)
            approval, declined = self.redteam_approval, self.redteam_declined
            self.redteam_phase = "NONE"
            if approval is None:
                code = "REDTEAM_DECLINED" if declined else "REDTEAM_APPROVAL_TIMEOUT"
                return {"status": "SKIPPED", "code": code, "open": previous}
            self.phase = "sbx_redteam"
            self.redteam_phase = "RUNNING"
            try:
                return await act(
                    "sbx_redteam",
                    {
                        "input": data,
                        "plan": plan,
                        "requirements_ref": requirements_ref,
                        "suite_ref": suite_ref,
                        "approval": approval,
                        "previous_findings": previous,
                        "digest": digest,
                        **ctx(run, self.attempt),
                    },
                    # The attack's own budget plus what it needs to put things back.
                    minutes=int(data["redteam"]["time_budget_seconds"] / 60) + 15,
                    attempts=1,  # a yes is used up by one run: a retry would only be refused
                )
            finally:
                self.redteam_phase = "NONE"

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
        digest: str | None = None
        review = False
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
                    **ctx(run, 1),
                },
                minutes=30,
            )
            builds_ref = built.get("builds_ref")
            plan, previous = None, None
            if built.get("build_error"):
                # The real build path produced no image: a person looks at it, no Sandbox exists.
                reasons = [f"BUILD:{built['build_error']}"]
                outcome = "NEEDS_REVIEW"
                await emit_attempt(run, 1, {"passed": False, "reasons": reasons}, "BUILD", None)
            else:
                images, built_sha = built["images"], built.get("source_sha256")
                digest = image_digest(images)
                self.phase = "sbx_prepare"
                prepared = True
                prep = await act(
                    "sbx_prepare",
                    {
                        "input": data,
                        "requirements_ref": requirements_ref,
                        "images": images,
                        **ctx(run, 1),
                    },
                )
                plan = prep["plan"]
                if plan is None:
                    reasons = [f"SANDBOX:{prep['sandbox_error']}"]
                    outcome = "NEEDS_REVIEW"
            infra_streak, tests_ran, baseline_ok = 0, False, False
            ai_pending = True
            self.attempt = 1
            first_run = True
            while plan is not None:
                if not first_run:
                    run += 1
                first_run = False
                self.phase = "sbx_apply"
                applied = await act(
                    "sbx_apply",
                    {
                        "input": data,
                        "plan": plan,
                        "previous_plan": previous,
                        "attempt": self.attempt,
                        "baseline_ok": baseline_ok,
                        **ctx(run, self.attempt),
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
                            **ctx(run, self.attempt),
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
                        **ctx(run, self.attempt),
                    },
                    minutes=15,
                )
                tests_ran = tests_ran or applied.get("sandbox_error") is None
                self.verdict = verified["verdict"]
                checks = verified.get("checks")
                security_round = False
                if self.verdict["passed"]:
                    last_results = verified["results"]
                    if red_on and digest is not None:
                        # Ticket 37: the Sandbox passed its tests; now it is attacked, once per
                        # image, and only with a yes. A Finding goes to the Fixer; the image it
                        # produces is checked again for that Finding only.
                        report = await redteam_round(plan, digest, red_open)
                        if report["status"] == "DONE":
                            fixed_before = bool(red_open)
                            red_open = report["open"]
                            state = "CLEAN" if not fixed_before else "FIXED"
                            red_result = red_report(
                                state if not red_open else "OPEN", report.get("code"), red_open,
                                resolved=report.get("resolved", []),
                                withheld=report.get("withheld", []),
                                summary=report.get("summary"),
                            )  # fmt: skip
                            if red_open and sec_attempts < MAX_SECURITY_ATTEMPTS:
                                security_round = True
                            elif red_open:
                                red_result["status"] = "STILL_OPEN"
                        else:
                            red_result = red_report(
                                report["status"], report.get("code"), red_open,
                                withheld=report.get("withheld", []),
                            )  # fmt: skip
                if self.verdict["passed"] and not security_round:
                    self.attempts.append({"attempt": self.attempt, "passed": True})
                    outcome = "PASSED"
                    await emit_attempt(run, self.attempt, self.verdict, None, checks)
                    break
                if security_round:
                    # R4: a security fix has its own count, apart from the functional three.
                    sec_attempts += 1
                    self.phase = "sbx_classify"
                    classified = await act(
                        "sbx_classify",
                        {
                            "plan": plan,
                            "requirements_ref": requirements_ref,
                            "verified": verified,
                            "open_findings": [f["finding_id"] for f in red_open],
                        },
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
                else:
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
                    await emit_attempt(run, self.attempt, self.verdict, klass, checks)
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
                    if self.attempt - sec_attempts >= 3:
                        outcome = "NEEDS_REVIEW"
                        review = True
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
                        **(
                            {
                                "findings": [
                                    {
                                        k: v
                                        for k, v in f.items()
                                        if k not in ("repro", "evidence_ref")
                                    }
                                    for f in red_open
                                ]
                            }
                            if security_round
                            else {}
                        ),
                        **ctx(run, self.attempt),
                    },
                    minutes=20,
                )
                if fix.get("fix_ids"):
                    await emit_attempt(
                        run, self.attempt, self.verdict, klass, checks, fix["fix_ids"], "fixed"
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
                            **ctx(run + 1, self.attempt),
                        },
                        minutes=30,
                    )
                    if rebuilt.get("build_error"):
                        reasons = [f"BUILD:{rebuilt['build_error']}"]
                        outcome = "NEEDS_REVIEW"
                        run += 1
                        verdict = {"passed": False, "reasons": reasons}
                        await emit_attempt(run, self.attempt, verdict, "BUILD", None)
                        break
                    images, built_sha = rebuilt["images"], rebuilt.get("source_sha256")
                    digest = image_digest(images)
                    builds_ref = rebuilt.get("builds_ref") or builds_ref
                prep = await act(
                    "sbx_prepare",
                    {
                        "input": data,
                        "requirements_ref": requirements_ref,
                        "images": images,
                        "previous_plan": plan,
                        **ctx(run + 1, self.attempt),
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
                if not failed:
                    await emit_done("PENDING", digest if outcome == "PASSED" else None)
                try:
                    await asyncio.shield(
                        act("sbx_teardown", {"namespace": namespace, "wait": False}, minutes=10)
                    )
                except ActivityError:
                    self.cleanup_failed = True
                    if not failed:
                        raise
        # A run that never reached a Sandbox has nothing to clean up.
        await emit_done("CLEANED", digest if outcome == "PASSED" else None)
        if review:
            data_review = {"reason": "max_attempts", "attempts": self.attempt}
            key = event_key(run, self.attempt, "review", "waiting")
            await emit(("build.review", "waiting", data_review, key))
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
                await emit_done("CLEANED", None)
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
                    await emit_done("CLEANED", None)
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
            **({"redteam": red_result} if red_result is not None else {}),
            **({"builds_ref": builds_ref} if builds_ref is not None else {}),
            **({"suite_ref": suite_ref} if suite_ref != data["suite_ref"] else {}),
        }
