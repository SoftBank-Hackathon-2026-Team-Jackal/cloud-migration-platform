"""`execute_gcp_plan`: Apply an approved GCP INFRASTRUCTURE Plan once (ticket 10).

Mirrors `execute_aws_plan`: reloads the Plan Revision from the business DB, requires the claimed
manifest Digest, applies only the pinned saved Plan and heartbeats while Terraform runs.
"""

from pathlib import Path

from cmp_api.approvals import PlanRevision
from cmp_contracts.plan import PlanManifest, Preview
from cmp_providers.aws_apply import Runner, subprocess_runner
from cmp_providers.gcp_apply import AdcLookup, apply_gcp_infrastructure, default_adc_email
from cmp_providers.plans import manifest_digest
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity

from cmp_worker.activities.terraform_run import run_with_heartbeat
from cmp_worker.provisioning_events import log_result, terraform_progress

HEARTBEAT_SECONDS = 30


def _failed(code: str) -> dict:
    return {"status": "FAILED", "code": code, "resource_refs": {}}


class GcpInfraActivities:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        artifact_root: Path | str | None,
        aws_profile: str | None,
        gcloud_configuration: str | None,
        adc_email: str | None,
        terraform_bin: str = "terraform",
        runner: Runner = subprocess_runner,
        adc_lookup: AdcLookup = default_adc_email,
    ):
        self._session_factory = session_factory
        self._artifact_root = Path(artifact_root) if artifact_root else None
        self._aws_profile = aws_profile
        self._gcloud_configuration = gcloud_configuration
        self._adc_email = adc_email
        self._terraform_bin = terraform_bin
        self._runner = runner
        self._adc_lookup = adc_lookup

    def _apply(self, request: dict) -> dict:
        if self._artifact_root is None:
            return _failed("ARTIFACT_ROOT_NOT_CONFIGURED")
        with self._session_factory() as session:
            row = session.get(PlanRevision, (request["plan_id"], request["revision"]))
            if row is None:
                return _failed("PLAN_NOT_FOUND")
            manifest = PlanManifest.model_validate(row.manifest)
            preview = Preview.model_validate(row.preview)
        if manifest_digest(manifest) != request["manifest_digest"]:
            return _failed("MANIFEST_DIGEST_MISMATCH")
        result = apply_gcp_infrastructure(
            manifest,
            preview,
            self._artifact_root / f"{request['plan_id']}-r{request['revision']}",
            aws_profile=self._aws_profile or "",
            gcloud_configuration=self._gcloud_configuration or "",
            expected_adc_email=self._adc_email or "",
            terraform_bin=self._terraform_bin,
            runner=self._runner,
            adc_email=self._adc_lookup,
            progress=terraform_progress(manifest.migration_id, preview),  # ticket 24
        )
        return log_result(manifest.migration_id, "execute_gcp_plan", result)

    @activity.defn(name="execute_gcp_plan")
    async def execute_gcp_plan(self, request: dict) -> dict:
        # Ticket 53 (D9): heartbeats; a cancellation stops Terraform gracefully first.
        return await run_with_heartbeat(self._apply, request, heartbeat_seconds=HEARTBEAT_SECONDS)
