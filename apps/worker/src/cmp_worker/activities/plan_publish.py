"""`publish_migration_plan`: the MigrationWorkflow `planning` step (ticket 18, P7).

Input `{migration_id}`; output small codes/identifiers only. Without a configured platform
ArtifactStore the Activity reports `SCAFFOLD`, and the Workflow keeps the scaffold path.
"""

import asyncio

from temporalio import activity

from cmp_worker.plan_publish import PlanPublisher

HEARTBEAT_SECONDS = 30


class PlanPublishActivities:
    def __init__(self, publisher: PlanPublisher | None):
        self._publisher = publisher

    @activity.defn(name="publish_migration_plan")
    async def publish_migration_plan(self, request: dict) -> dict:
        if self._publisher is None:
            return {"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"}
        activity.heartbeat("started")
        work = asyncio.create_task(
            asyncio.to_thread(self._publisher.publish, request["migration_id"])
        )
        while not work.done():
            await asyncio.wait({work}, timeout=HEARTBEAT_SECONDS)
            if not work.done():
                activity.heartbeat("planning")  # Terraform Plan can take minutes
        return work.result()


def build_plan_publish_activities(session_factory) -> PlanPublishActivities:
    """Worker wiring: ARTIFACT_STORE_DIR (platform store) enables the real path."""
    from pathlib import Path

    from cmp_api.config import settings as api_settings
    from cmp_api.onprem_rollout import LocalArtifactStore

    from cmp_worker.config import settings

    if not api_settings.artifact_store_dir:
        return PlanPublishActivities(None)
    root = settings.provisioning_artifact_root
    from cmp_worker.provisioning_events import PlanReadyEvents

    return PlanPublishActivities(
        PlanReadyEvents(  # ticket 24: `plan.ready` after each successful publication
            PlanPublisher(
                session_factory,
                LocalArtifactStore(Path(api_settings.artifact_store_dir)),
                cloud_artifact_root=Path(root) if root else None,
                aws_profile=settings.provisioning_aws_profile,
                gcloud_configuration=settings.provisioning_gcloud_configuration,
                gcp_adc_email=settings.provisioning_gcp_adc_email,
                terraform_bin=settings.terraform_bin,
            )
        )
    )
