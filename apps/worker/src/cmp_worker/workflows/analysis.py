import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import ANALYSIS_QUEUE

    from cmp_worker.activities import analysis
    from cmp_worker.review_gate import ReviewGate


@workflow.defn
class AnalysisWorkflow:
    def __init__(self) -> None:
        self.gate: ReviewGate | None = None
        self.integrated = False
        self.early_rejections: list[dict[str, str]] = []

    @workflow.signal
    def confirm_requirements(self, payload: dict) -> None:
        if self.gate is None:
            self.early_rejections.append(
                {"signal": "confirm_requirements", "code": "NOT_READY", "detail": ""}
            )
            return
        self.gate.confirm(payload)

    @workflow.signal
    def approve_losses(self, payload: dict) -> None:
        if self.gate is None:
            self.early_rejections.append(
                {"signal": "approve_losses", "code": "NOT_READY", "detail": ""}
            )
            return
        self.gate.approve(payload)

    @workflow.query
    def review_state(self) -> dict:
        if self.gate is None:
            return {
                "phase": "ANALYZING",
                "analysis_ref": None,
                "pending": None,
                "items": [],
                "security": [],
                "blocked": [],
                "rejections": list(self.early_rejections),
            }
        view = self.gate.view()
        view["rejections"] = [*self.early_rejections, *view["rejections"]]
        return view

    async def _exec(self, fn, value, *, minutes=1, attempts=2):
        return await workflow.execute_activity(
            fn,
            value,
            task_queue=ANALYSIS_QUEUE,
            start_to_close_timeout=timedelta(minutes=minutes),
            retry_policy=RetryPolicy(maximum_attempts=attempts),
        )

    async def _review(self, report: dict, requirements) -> dict:
        rows = report["mapping"]
        # Ticket 29-B: a security level that goes down is a loss to approve, like a lost feature.
        # Patched so a run that started before it keeps the behaviour its history recorded.
        security = (
            report.get("security_judgements") if workflow.patched("security-gate-v1") else None
        )
        if not rows and not any(j["verdict"] == "LOWER" for j in security or []):
            return {"report": report, "requirements": requirements, "review": None}
        # Stop here until a person confirms required features and approves losses.
        # There is no timeout: a missing decision never turns into an approval.
        self.gate = ReviewGate(report["ref"], rows, security=security)
        await workflow.wait_condition(lambda: self.gate.finished)
        if self.gate.phase == "BLOCKED":
            raise ApplicationError(
                "Required features cannot be migrated",
                self.gate.view(),
                type="ANALYSIS_BLOCKED",
                non_retryable=True,
            )
        review = await self._exec(
            "record_review" if self.integrated else analysis.record_review,
            {
                **({"migration_id": report["migration_id"]} if self.integrated else {}),
                "analysis_ref": report["ref"],
                **self.gate.decisions(),
            },
        )
        report = {**report, "mapping": [r.model_dump(mode="json") for r in self.gate.rows]}
        return {"report": report, "requirements": requirements, "review": review}

    @workflow.run
    async def run(self, data: str | dict) -> dict:
        # A string is the scaffold input (migration ID only); a dict is a real `AnalysisInput`.
        if isinstance(data, str):
            return await self._run_scaffold(data)
        self.integrated = True
        return await self._run_integrated(data)

    async def _run_integrated(self, inp: dict) -> dict:
        source = await self._exec("fetch_source", inp, minutes=10, attempts=3)
        scan = {"input": inp, "source": source}
        code, manifests, infra = await asyncio.gather(
            self._exec("scan_code", scan, minutes=5, attempts=3),
            self._exec("scan_manifests", scan, minutes=5, attempts=3),
            self._exec("scan_aws_infra", inp, minutes=5, attempts=3),
        )
        built = await self._exec(
            "build_requirements",
            {
                "input": inp,
                "source": source,
                "code_ref": code,
                "manifests_ref": manifests,
                "infra": infra,
            },
        )
        if built["requirements_ref"] is None:
            raise ApplicationError(
                "Requirements could not be built",
                {"blocks": built["blocks"], "unmapped_items": built["unmapped_items"]},
                type="ANALYSIS_BLOCKED",
                non_retryable=True,
            )
        pre = await self._exec(
            "precheck",
            {"input": inp, "source": source, "code_ref": code, "manifests_ref": manifests},
        )
        mapped = await self._exec(
            "map_usage",
            {
                "input": inp,
                "source": source,
                "code_ref": code,
                "manifests_ref": manifests,
                "built": built,
                "precheck": pre,
            },
            minutes=15,
        )
        if mapped["status"] == "NEEDS_REVIEW":
            raise ApplicationError(
                "Mapping needs a person to review it",
                {"reason": mapped.get("reason")},
                type="MAPPING_NEEDS_REVIEW",
                non_retryable=True,
            )
        report = await self._exec(
            "check_evidence",
            {
                "input": inp,
                "source": source,
                "built": built,
                "infra": infra,
                "precheck": pre,
                "mapped": mapped,
            },
        )
        report = {**report, "migration_id": inp["migration_id"]}
        result = await self._review(report, built["requirements_ref"])
        # The Sandbox input is built from the workspace this worker checked out (ticket 21).
        return {
            **result,
            "source": {"workspace": source["workspace"], "commit": source["commit"]},
            "assembly_ref": built["assembly_ref"],
        }

    async def _run_scaffold(self, migration_id: str) -> dict:
        async def run_activity(fn, value=migration_id, *, minutes=1, attempts=2):
            return await self._exec(fn, value, minutes=minutes, attempts=attempts)

        await run_activity(analysis.fetch_source, minutes=2, attempts=3)
        await asyncio.gather(
            *(
                run_activity(fn, minutes=3, attempts=3)
                for fn in (
                    analysis.scan_code,
                    analysis.scan_manifests,
                    analysis.scan_aws_infra,
                    analysis.scan_network,
                )
            )
        )
        requirements = await run_activity(analysis.build_requirements)
        await run_activity(analysis.precheck)
        await run_activity(analysis.map_usage, minutes=2)
        report = await run_activity(
            analysis.check_evidence,
            {"migration_id": migration_id, "requirements": requirements},
        )
        return await self._review(report, requirements)
