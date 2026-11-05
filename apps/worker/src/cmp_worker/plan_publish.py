"""Planning input v2 → provider Plan/Preview → published Plan Revision (ticket 18).

ON_PREM: 01A validation, 04B render, Chart packaging, 12A Preview, platform ArtifactStore.
AWS/GCP: 01A validation, then the 05/06 saved Terraform Plan and Preview (INFRASTRUCTURE).
Results hold codes and identifiers only; never Secret values, Terraform output or input text.
"""

from collections.abc import Callable
from pathlib import Path

from cmp_api.agent_registry import RegisteredAgent
from cmp_api.approvals import (
    ApprovalError,
    PlanPublication,
    PlanRevision,
    latest_revision,
    publish_plan_revision,
)
from cmp_api.onprem_rollout import LocalArtifactStore, RolloutError
from cmp_api.plan_inputs import (
    PlanInputError,
    cloud_plan_input,
    input_digest,
    load_planning_input,
    template_version,
)
from cmp_contracts.plan import OnPremIdentity, PlanManifest, Preview
from cmp_contracts.plan_publish import (
    OnPremTargetSettings,
    PlanningInputRegistration,
    plan_id_for,
)
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_contracts.provisioning_v2 import (
    ApplicationRequirement,
    PostgresRequirement,
    ProvisioningSpecV2,
)
from cmp_providers.artifacts import canonical_json_bytes, sha256_json
from cmp_providers.aws_preview import plan_aws_infrastructure
from cmp_providers.chart_package import package_chart
from cmp_providers.gcp_preview import plan_gcp_infrastructure
from cmp_providers.onprem_preview import onprem_preview
from cmp_providers.plans import manifest_digest
from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cmp_worker.provisioning_v2 import prepare_plan_v2

ROOT = Path(__file__).resolve().parents[4]
DB_CHART = ROOT / "infra/helm/postgres"
APP_CHART = ROOT / "infra/helm/application"


class PublishFailure(Exception):
    def __init__(self, code: str, issues: list[str] | None = None):
        super().__init__(code)
        self.code = code
        self.issues = sorted(set(issues or [code]))


def _codes(error: ProvisioningInputError) -> list[str]:
    return [issue.code for issue in error.issues]


def _release(resource_id: str) -> str:
    return resource_id.lower().replace("_", "-")


def deployment_request(spec: ProvisioningSpecV2, target: OnPremTargetSettings) -> dict:
    """P4: the 04B request for the P0 topology (one PostgreSQL, one application)."""
    resources = spec.requirements.resources
    databases = [r for r in resources if isinstance(r, PostgresRequirement)]
    apps = [r for r in resources if isinstance(r, ApplicationRequirement)]
    if len(databases) != 1 or len(apps) != 1:
        raise PublishFailure("UNSUPPORTED_TOPOLOGY")
    [database], [app] = databases, apps
    [image] = [i for i in spec.images if i.resource_id == app.id]
    db_release = _release(database.id)
    return {
        "contract_version": "2",
        "environment": "PRODUCTION",
        "provider": "ON_PREM",
        "namespace": target.namespace,
        "resources": [
            {
                "id": database.id,
                "kind": "POSTGRESQL",
                "release_name": db_release,
                "image": {"uri": target.database_image, "platforms": [image.platform]},
                "version": "16",
                "extensions": [],
                "database": {
                    "name": database.database.name,
                    "username": database.database.username,
                    "password_secret_ref": {"name": f"{db_release}-auth", "key": "password"},
                },
                "resources": {
                    "cpu_millicores": database.cpu_millicores,
                    "memory_mib": database.memory_mib,
                },
                "storage": {"class_name": target.storage_class, "size_gib": database.storage_gib},
            },
            {
                "id": app.id,
                "kind": "APPLICATION",
                "release_name": _release(app.id),
                "image": {"uri": image.image_uri, "platforms": [image.platform]},
                "replicas": app.replicas,
                "resources": {"cpu_millicores": app.cpu_millicores, "memory_mib": app.memory_mib},
                "port": app.port,
                "health_path": app.health_path,
                "env_bindings": {
                    name: binding.model_dump(mode="json")
                    for name, binding in app.env_bindings.items()
                },
            },
        ],
    }


class PlanPublisher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        store: LocalArtifactStore,
        *,
        cloud_artifact_root: Path | None = None,
        aws_profile: str | None = None,
        gcloud_configuration: str | None = None,
        gcp_adc_email: str | None = None,
        terraform_bin: str = "terraform",
        helm_bin: str = "helm",
        aws_template_dir: Path | None = None,
        gcp_template_dir: Path | None = None,
    ):
        self._sessions = session_factory
        self._store = store
        self._cloud_root = Path(cloud_artifact_root) if cloud_artifact_root else None
        self._aws_profile = aws_profile
        self._gcloud = gcloud_configuration
        self._adc_email = gcp_adc_email
        self._terraform = terraform_bin
        self._helm = helm_bin
        self._aws_template = aws_template_dir
        self._gcp_template = gcp_template_dir

    # -- entry point -----------------------------------------------------------------------

    def publish(self, migration_id: str) -> dict:
        try:
            return self._publish(migration_id)
        except PublishFailure as failure:
            return {"status": "FAILED", "code": failure.code, "issues": failure.issues}

    def _publish(self, migration_id: str) -> dict:
        with self._sessions() as session:
            registration = load_planning_input(session, migration_id)
            if registration is None:
                raise PublishFailure("PLANNING_INPUT_MISSING")
            plan_id = plan_id_for(migration_id)
            digest = input_digest(registration)
            latest = latest_revision(session, plan_id)
            if latest is not None:
                published = session.get(PlanPublication, (plan_id, latest))
                if published is not None and published.input_digest == digest:
                    return self._result(session, plan_id, latest, "PLAN_REUSED")
            revision = (latest or 0) + 1
            spec, pending = self._validated(registration)
            plan = {
                "plan_id": plan_id,
                "revision": revision,
                "template_version": template_version(registration),
            }
            if registration.target.provider == "ON_PREM":
                manifest, preview = self._onprem(session, registration, spec, plan)
            else:
                manifest, preview = self._cloud(registration, plan)
            if preview.errors:
                raise PublishFailure("PREVIEW_ERRORS", [i.code for i in preview.errors])
            preview = preview.model_copy(
                update={"pending_decisions": [*preview.pending_decisions, *pending]}
            )
            try:
                publish_plan_revision(session, manifest, preview, input_digest=digest)
            except (ApprovalError, IntegrityError):
                # A concurrent run published this Revision first; report the stored one.
                session.rollback()
                raise PublishFailure("REVISION_CONFLICT") from None
            return self._result(session, plan_id, revision, "PLAN_PUBLISHED")

    @staticmethod
    def _result(session: Session, plan_id: str, revision: int, code: str) -> dict:
        row = session.get(PlanRevision, (plan_id, revision))
        manifest = PlanManifest.model_validate(row.manifest)
        return {
            "status": "PUBLISHED",
            "code": code,
            "plan_id": plan_id,
            "revision": revision,
            "provider": manifest.target.provider,
            "scope": manifest.scope,
            "manifest_digest": manifest_digest(manifest),
            "preview_digest": sha256_json(row.preview),
        }

    def _validated(self, registration: PlanningInputRegistration):
        request = registration.planning_request
        try:
            content = self._store.read_raw(request.analysis.requirements_ref.sha256)
        except RolloutError:
            raise PublishFailure("REQUIREMENTS_UNAVAILABLE") from None
        try:
            result = prepare_plan_v2(request.model_dump(mode="json"), requirements_bytes=content)
        except ProvisioningInputError as error:
            raise PublishFailure("PLANNING_INPUT_INVALID", _codes(error)) from None
        return result.spec, result.pending_decisions

    # -- ON_PREM ---------------------------------------------------------------------------

    def _onprem(self, session, registration, spec, plan) -> tuple[PlanManifest, Preview]:
        target: OnPremTargetSettings = registration.target
        agent = session.get(RegisteredAgent, target.agent_id)
        if agent is None:
            raise PublishFailure("AGENT_NOT_REGISTERED")
        if agent.revoked_at is not None:
            raise PublishFailure("AGENT_REVOKED")
        if agent.cluster_uid != target.cluster_uid:
            raise PublishFailure("CLUSTER_UID_MISMATCH")
        deployment = deployment_request(spec, target)
        try:
            rendered = render_shared_deployment_v2(
                deployment,
                database_chart_dir=DB_CHART,
                application_chart_dir=APP_CHART,
                helm_bin=self._helm,
            )
            db_chart = package_chart(DB_CHART, helm_bin=self._helm)
            app_chart = package_chart(APP_CHART, helm_bin=self._helm)
        except ProvisioningInputError as error:
            raise PublishFailure("RENDER_FAILED", _codes(error)) from None
        db = next(r for r in rendered.resources if r.kind == "POSTGRESQL")
        app = next(r for r in rendered.resources if r.kind == "APPLICATION")
        spec_document = {
            "spec": spec.model_dump(mode="json"),
            "target": target.model_dump(mode="json"),
            "deployment": deployment,
        }
        contents = {
            "spec": ("SPEC", canonical_json_bytes(spec_document)),
            "database_chart": ("CHART", db_chart),
            "database_values": (
                "VALUES",
                canonical_json_bytes(db.helm_values.model_dump(mode="json")),
            ),
            "application_chart": ("CHART", app_chart),
            "application_values": (
                "VALUES",
                canonical_json_bytes(app.helm_values.model_dump(mode="json")),
            ),
            "bindings": ("BINDINGS", canonical_json_bytes({"bindings": []})),
        }
        prefix = f"{plan['plan_id']}-r{plan['revision']}"
        artifacts = {
            role: self._store.put(content, ref_id=f"{prefix}-{role}", kind=kind)
            for role, (kind, content) in contents.items()
        }
        manifest = PlanManifest(
            contract_version="1",
            digest_format="cmp-json-v1",
            migration_id=spec.migration_id,
            plan=plan,
            target=OnPremIdentity(
                provider="ON_PREM",
                environment_id=target.environment_id,
                agent_id=target.agent_id,
                cluster_ref={"id": target.cluster_uid, "version": "1"},
                namespace=target.namespace,
            ),
            scope="WORKLOADS",
            image_uri=deployment["resources"][1]["image"]["uri"],
            artifacts=artifacts,
        )
        try:
            preview = onprem_preview(manifest, {role: c for role, (_, c) in contents.items()})
        except ProvisioningInputError as error:
            raise PublishFailure("PREVIEW_FAILED", _codes(error)) from None
        return manifest, preview

    # -- AWS / GCP -------------------------------------------------------------------------

    def _cloud(self, registration, plan) -> tuple[PlanManifest, Preview]:
        if self._cloud_root is None:
            raise PublishFailure("PLAN_FAILED", ["ARTIFACT_ROOT_NOT_CONFIGURED"])
        try:
            request = cloud_plan_input(registration, plan)
        except PlanInputError as error:
            raise PublishFailure("PLAN_FAILED", [error.code]) from None
        folder = self._cloud_root / f"{plan['plan_id']}-r{plan['revision']}"
        if folder.exists() or folder.is_symlink():
            return self._recover(folder, request)
        planner = self._planner(registration.target.provider)
        try:
            return planner(request)
        except ProvisioningInputError as error:
            raise PublishFailure("PLAN_FAILED", _codes(error)) from None

    def _planner(self, provider: str) -> Callable:
        root = self._cloud_root
        if provider == "AWS":
            return lambda request: plan_aws_infrastructure(
                request,
                output_dir=root,
                terraform_bin=self._terraform,
                aws_profile=self._aws_profile or "",
                template_dir=self._aws_template,
                backend_mode="S3",
            )
        return lambda request: plan_gcp_infrastructure(
            request,
            output_dir=root,
            mode="S3",
            gcloud_configuration=self._gcloud,
            expected_adc_email=self._adc_email,
            aws_profile=self._aws_profile,
            terraform_bin=self._terraform,
            template_dir=self._gcp_template,
        )

    @staticmethod
    def _recover(folder: Path, request) -> tuple[PlanManifest, Preview]:
        """P3: a Worker planned this Revision but died before the DB commit."""
        try:
            same = (folder / "spec.json").read_bytes() == canonical_json_bytes(
                request.model_dump(mode="json")
            )
            manifest = PlanManifest.model_validate_json((folder / "manifest.json").read_bytes())
            preview = Preview.model_validate_json((folder / "preview.json").read_bytes())
        except (OSError, ValueError):
            raise PublishFailure("PLAN_DIRECTORY_CONFLICT") from None
        if not same or preview.manifest_digest != manifest_digest(manifest):
            raise PublishFailure("PLAN_DIRECTORY_CONFLICT")
        return manifest, preview
