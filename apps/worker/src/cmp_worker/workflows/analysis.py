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
                "blocked": [],
                "rejections": list(self.early_rejections),
            }
        view = self.gate.view()
        view["rejections"] = [*self.early_rejections, *view["rejections"]]
        return view

    @workflow.run
    async def run(self, migration_id: str) -> dict:
        async def run_activity(fn, value=migration_id, *, minutes=1, attempts=2):
            return await workflow.execute_activity(
                fn,
                value,
                task_queue=ANALYSIS_QUEUE,
                start_to_close_timeout=timedelta(minutes=minutes),
                retry_policy=RetryPolicy(maximum_attempts=attempts),
            )

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
            {
                "migration_id": migration_id,
                "requirements": requirements,
            },
        )
        rows = report["mapping"]
        if not rows:
            return {"report": report, "requirements": requirements, "review": None}
        # Stop here until a person confirms required features and approves losses.
        # There is no timeout: a missing decision never turns into an approval.
        self.gate = ReviewGate(report["ref"], rows)
        await workflow.wait_condition(lambda: self.gate.finished)
        if self.gate.phase == "BLOCKED":
            raise ApplicationError(
                "Required features cannot be migrated",
                self.gate.view(),
                type="ANALYSIS_BLOCKED",
                non_retryable=True,
            )
        review = await run_activity(
            analysis.record_review, {"analysis_ref": report["ref"], **self.gate.decisions()}
        )
        report = {**report, "mapping": [r.model_dump(mode="json") for r in self.gate.rows]}
        return {"report": report, "requirements": requirements, "review": review}
