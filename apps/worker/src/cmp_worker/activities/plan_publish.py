"""Plan publication Activities for the MigrationWorkflow (tickets 18 P7 and 22 W8).

`publish_migration_plan` input `{migration_id}`; `publish_workload_plan` input
`{migration_id, infrastructure: {plan_id, revision}}` (the executed AWS/GCP infra Revision).
Outputs are small codes/identifiers only. Without a configured platform ArtifactStore the
Activities report `SCAFFOLD`, and the Workflow keeps the scaffold path.
"""

import asyncio

from temporalio import activity

from cmp_worker.plan_publish import PlanPublisher

HEARTBEAT_SECONDS = 30
SCAFFOLD = {"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"}


class PlanPublishActivities:
    def __init__(self, publisher: PlanPublisher | None):
        self._publisher = publisher

    @activity.defn(name="publish_migration_plan")
    async def publish_migration_plan(self, request: dict) -> dict:
        if self._publisher is None:
            return SCAFFOLD
        return await self._run(self._publisher.publish, request["migration_id"])

    @activity.defn(name="publish_workload_plan")
    async def publish_workload_plan(self, request: dict) -> dict:
        if self._publisher is None:
            return SCAFFOLD
        return await self._run(
            self._publisher.publish_workloads, request["migration_id"], request["infrastructure"]
        )

    @staticmethod
    async def _run(fn, *args) -> dict:
        activity.heartbeat("started")
        work = asyncio.create_task(asyncio.to_thread(fn, *args))
        while not work.done():
            await asyncio.wait({work}, timeout=HEARTBEAT_SECONDS)
            if not work.done():
                activity.heartbeat("planning")  # Terraform Plan / Helm render can take minutes
        return work.result()


def build_plan_publish_activities(session_factory) -> PlanPublishActivities:
    """Worker wiring: ARTIFACT_STORE_DIR (platform store) enables the real path."""
    from pathlib import Path

    from cmp_api.config import settings as api_settings
    from cmp_api.env_store import optional_env_file_store
    from cmp_api.onprem_rollout import LocalArtifactStore

    from cmp_worker.config import settings

    if not api_settings.artifact_store_dir:
        return PlanPublishActivities(None)
    root = settings.provisioning_artifact_root
    from cmp_worker.provisioning_events import PlanEvents

    return PlanPublishActivities(
        PlanEvents(  # tickets 24/60: planning + approval events after each publication
            PlanPublisher(
                session_factory,
                LocalArtifactStore(Path(api_settings.artifact_store_dir)),
                cloud_artifact_root=Path(root) if root else None,
                aws_profile=settings.provisioning_aws_profile,
                gcloud_configuration=settings.provisioning_gcloud_configuration,
                gcp_adc_email=settings.provisioning_gcp_adc_email,
                terraform_bin=settings.terraform_bin,
                env_store=optional_env_file_store(),  # ENV-04: names only at Plan time
                platform_egress_cidrs=settings.platform_egress_cidrs(),  # ticket 47
            )
        )
    )
