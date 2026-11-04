import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from cmp_contracts.queues import ANALYSIS_QUEUE

    from cmp_worker.activities import analysis


@workflow.defn
class AnalysisWorkflow:
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
        return {"report": report, "requirements": requirements}
