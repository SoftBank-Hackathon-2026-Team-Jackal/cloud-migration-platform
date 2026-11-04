"""`execute_aws_plan`: Apply an approved AWS INFRASTRUCTURE Plan once (ticket 08).

Runs after `claim_approved_execution`. Reloads the Plan Revision from the business DB, requires
it to match the claimed manifest Digest, and applies only the saved Plan pinned by that manifest.
Heartbeats while Terraform runs; the Workflow never retries this Activity.
"""

import asyncio
from pathlib import Path

from cmp_api.approvals import PlanRevision
from cmp_contracts.plan import PlanManifest, Preview
from cmp_providers.aws_apply import Runner, apply_aws_infrastructure, subprocess_runner
from cmp_providers.plans import manifest_digest
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity

HEARTBEAT_SECONDS = 30


class AwsInfraActivities:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        artifact_root: Path | str | None,
        aws_profile: str | None,
        terraform_bin: str = "terraform",
        runner: Runner = subprocess_runner,
    ):
        self._session_factory = session_factory
        self._artifact_root = Path(artifact_root) if artifact_root else None
        self._aws_profile = aws_profile
        self._terraform_bin = terraform_bin
        self._runner = runner

    def _apply(self, request: dict) -> dict:
        if self._artifact_root is None:
            return {"status": "FAILED", "code": "ARTIFACT_ROOT_NOT_CONFIGURED", "resource_refs": {}}
        with self._session_factory() as session:
            row = session.get(PlanRevision, (request["plan_id"], request["revision"]))
            if row is None:
                return {"status": "FAILED", "code": "PLAN_NOT_FOUND", "resource_refs": {}}
            manifest = PlanManifest.model_validate(row.manifest)
            preview = Preview.model_validate(row.preview)
        if manifest_digest(manifest) != request["manifest_digest"]:
            return {"status": "FAILED", "code": "MANIFEST_DIGEST_MISMATCH", "resource_refs": {}}
        directory = self._artifact_root / f"{request['plan_id']}-r{request['revision']}"
        return apply_aws_infrastructure(
            manifest,
            preview,
            directory,
            aws_profile=self._aws_profile or "",
            terraform_bin=self._terraform_bin,
            runner=self._runner,
        )

    @activity.defn(name="execute_aws_plan")
    async def execute_aws_plan(self, request: dict) -> dict:
        activity.heartbeat("started")
        work = asyncio.create_task(asyncio.to_thread(self._apply, request))
        while not work.done():
            await asyncio.wait({work}, timeout=HEARTBEAT_SECONDS)
            if not work.done():
                activity.heartbeat("running")
        return work.result()
