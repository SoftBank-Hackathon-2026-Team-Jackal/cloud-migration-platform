"""Planning input v2 → provider Plan/Preview → published Plan Revision (ticket 18).

ON_PREM: 01A validation, 04B render, Chart packaging, 12A Preview, platform ArtifactStore.
AWS/GCP: 01A validation, then the 05/06 saved Terraform Plan and Preview (INFRASTRUCTURE).
AWS/GCP after the infrastructure run (ticket 22): a separate WORKLOADS Plan for the 09/11 DB +
App deployment, built from the stored infrastructure result and the registration's workload
settings, with artifacts where 09/11 read them.
Results hold codes and identifiers only; never Secret values, Terraform output or input text.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cmp_api.agent_registry import RegisteredAgent
from cmp_api.approvals import (
    ApprovalError,
    PlanApproval,
    PlanExecution,
    PlanPublication,
    PlanRevision,
    latest_revision,
    publish_plan_revision,
)
from cmp_api.connections import ConnectionFailure, check_target_connection
from cmp_api.onprem_rollout import LocalArtifactStore, RolloutError
from cmp_api.plan_inputs import (
    PlanInputError,
    cloud_plan_input,
    env_plan_digest,
    input_digest,
    load_planning_input,
    template_version,
)
from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.env_plan import EnvPlan, EnvPlanEntry, EnvVarRef
from cmp_contracts.plan import OnPremIdentity, PlanManifest, Preview, ResourceChange
from cmp_contracts.plan_publish import (
    WORKLOAD_TEMPLATE,
    CloudWorkloadSettings,
    OnPremTargetSettings,
    PlanningInputRegistration,
    plan_id_for,
    workload_plan_id_for,
)
from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue
from cmp_contracts.provisioning_v2 import (
    ApplicationRequirement,
    PostgresRequirement,
    ProvisioningSpecV2,
)
from cmp_contracts.shared_deployment_v2 import SecretRefV2
from cmp_providers.app_renderer import ApplicationRenderError, render_application
from cmp_providers.application import prepare_application, resolve_application
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes, sha256_json
from cmp_providers.aws_credentials import connection_id_of
from cmp_providers.aws_preview import plan_aws_infrastructure
from cmp_providers.chart_package import package_chart
from cmp_providers.cloud_workload_preview import cloud_workload_preview
from cmp_providers.database_url import connection_url_secret_name
from cmp_providers.env_file import (
    DbOutputRefs,
    EnvFileError,
    EnvFileStore,
    app_env_secret_name,
    connection_secret_name,
    database_outputs,
    preview_env,
)
from cmp_providers.gcp_preview import plan_gcp_infrastructure
from cmp_providers.network_policy import TEMPLATE_VERSION as NETWORK_POLICY_TEMPLATE
from cmp_providers.network_policy import describe, production_policies, with_connections
from cmp_providers.onprem_preview import onprem_preview
from cmp_providers.plans import manifest_digest
from cmp_providers.resources import resource_root
from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cmp_worker.activities.aws_workloads import target_from_refs as aws_target
from cmp_worker.activities.gcp_workloads import target_from_refs as gcp_target
from cmp_worker.provisioning_events import PlanView
from cmp_worker.provisioning_v2 import prepare_plan_v2

ROOT = resource_root()
DB_CHART = ROOT / "infra/helm/postgres"
APP_CHART = ROOT / "infra/helm/application"
PLATFORMS = {"AWS": "EKS", "GCP": "GKE", "ON_PREM": "K3s"}
CLUSTER_TARGETS = {"AWS": aws_target, "GCP": gcp_target}  # 09 W11 / 11 K12 ref names
# v2 resource-output binding → v1 DATABASE env field read by 09/11 (W6).
DATABASE_FIELDS = {
    "host": "host",
    "port": "port",
    "name": "name",
    "username": "username",
    "password_secret_ref": "secret_ref",
    "connection_url_secret_ref": "connection_url_secret_ref",
}
ARTIFACT_KINDS = {
    "spec": "SPEC",
    "database_chart": "CHART",
    "database_values": "VALUES",
    "application_chart": "CHART",
    "application_values": "VALUES",
    "bindings": "BINDINGS",
    "network_policies": "MANIFEST",
}


class PublishFailure(Exception):
    def __init__(self, code: str, issues: list[str] | None = None):
        super().__init__(code)
        self.code = code
        self.issues = sorted(set(issues or [code]))


def _check_target_connection(session: Session, request) -> None:
    """Ticket 46 (C5): a connection `credential_ref` must be a verified target in the Plan account."""
    try:
        connection_id = connection_id_of(request.credential_ref)
        if connection_id is not None:
            check_target_connection(session, connection_id, account_id=request.account_id)
    except ProvisioningInputError as error:
        raise PublishFailure("PLAN_FAILED", _codes(error)) from None
    except ConnectionFailure as failure:
        raise PublishFailure("PLAN_FAILED", [failure.code]) from None


def _codes(error: ProvisioningInputError) -> list[str]:
    return [issue.code for issue in error.issues]


def _release(resource_id: str) -> str:
    return resource_id.lower().replace("_", "-")


def _topology(spec: ProvisioningSpecV2) -> tuple[PostgresRequirement, ApplicationRequirement]:
    resources = spec.requirements.resources
    databases = [r for r in resources if isinstance(r, PostgresRequirement)]
    apps = [r for r in resources if isinstance(r, ApplicationRequirement)]
    if len(databases) != 1 or len(apps) != 1:
        raise PublishFailure("UNSUPPORTED_TOPOLOGY")
    return databases[0], apps[0]


# REPLACED bindings that keep their existing platform step (DB password Secret, ticket 19 URL
# Secret); the other bindings and every PASS key are plain Secret references (ENV-04).
DB_OUTPUT_BINDINGS = {"password_secret_ref", "connection_url_secret_ref"}


@dataclass
class EnvRendering:
    """ENV-04 plan-time result of an EnvPlan: names, keys and codes only, never a value."""

    bindings: dict | None  # app v2 env_bindings; None → the requirements' bindings (blocked)
    pending: list[ValidationIssue] = field(default_factory=list)
    defaults: list[ResourceChange] = field(default_factory=list)
    platform_secrets: tuple[str, ...] = ()  # `-env`/`-conn` the executing side creates


def _env_binding(entry: EnvPlanEntry, item: EnvVarRef, database_id: str) -> dict:
    if entry.binding in DB_OUTPUT_BINDINGS:
        return {"resource_id": database_id, "output": entry.binding}
    return {"secret_ref": item.secret_ref.model_dump(mode="json")}


def _keys(plan: EnvPlan, disposition: str) -> list[str]:
    return sorted(e.key for e in plan.entries if e.disposition == disposition)


def _env_changes(
    plan: EnvPlan, app_release: str, db_release: str, rendered
) -> list[ResourceChange]:
    """Preview lines: key names and dispositions only (ENV-04). Nothing when blocked."""
    if rendered.decisions:
        return []
    changes = []
    passed = _keys(plan, "PASS")
    if passed:
        changes.append(
            ResourceChange(
                resource_type="APP",
                action="CREATE",
                name=f"앱 환경변수 Secret {app_env_secret_name(app_release)} 생성·갱신",
                size=f"PASS 키 {', '.join(passed)} (값은 실행 시 .env에서 읽고 Plan에 없음)",
            )
        )
    replaced = sorted((e.key, e.binding) for e in plan.entries if e.disposition == "REPLACED")
    if replaced:
        changes.append(
            ResourceChange(
                resource_type="DATABASE",
                action="CREATE",
                name="대상 DB 출력 연결(REPLACED, secretKeyRef)",
                size=", ".join(f"{key}→{binding}" for key, binding in replaced),
            )
        )
    conn = connection_secret_name(db_release)
    if any(item.secret_ref.name == conn for item in rendered.env):
        changes.append(
            ResourceChange(
                resource_type="DATABASE",
                action="CREATE",
                name=f"DB 연결 Secret {conn} 생성·갱신",
                size="host·port·name·username (플랫폼 관리)",
            )
        )
    excluded = _keys(plan, "EXCLUDED")
    if excluded:
        changes.append(
            ResourceChange(
                resource_type="APP",
                action="NO_CHANGE",
                name="대상에 복사하지 않는 .env 키(EXCLUDED)",
                size=", ".join(excluded),
            )
        )
    return changes


def deployment_request(
    spec: ProvisioningSpecV2,
    target: OnPremTargetSettings | CloudWorkloadSettings,
    provider: str = "ON_PREM",
    env_bindings: dict | None = None,
) -> dict:
    """P4: the 04B request for the P0 topology (one PostgreSQL, one application).

    `env_bindings` (ENV-04): the app's bindings rendered from the approved EnvPlan; replaces the
    requirements' bindings."""
    database, app = _topology(spec)
    [image] = [i for i in spec.images if i.resource_id == app.id]
    # Ticket 58: every platform the built image has, for both workloads. The pinned official
    # PostgreSQL image is a multi-architecture list, so the DB may sit on the same nodes.
    platforms = list(image.platforms)
    db_release = _release(database.id)
    return {
        "contract_version": "2",
        "environment": "PRODUCTION",
        "provider": provider,
        "namespace": target.namespace,
        "resources": [
            {
                "id": database.id,
                "kind": "POSTGRESQL",
                "release_name": db_release,
                "image": {"uri": target.database_image, "platforms": platforms},
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
                "image": {"uri": image.image_uri, "platforms": platforms},
                "replicas": app.replicas,
                "resources": {"cpu_millicores": app.cpu_millicores, "memory_mib": app.memory_mib},
                "port": app.port,
                "health_path": app.health_path,
                "env_bindings": (
                    env_bindings
                    if env_bindings is not None
                    else {
                        name: binding.model_dump(mode="json")
                        for name, binding in app.env_bindings.items()
                    }
                ),
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
        env_store: EnvFileStore | None = None,
        platform_egress_cidrs: tuple[str, ...] = (),
    ):
        self._sessions = session_factory
        self._egress = tuple(platform_egress_cidrs)
        self._store = store
        self._cloud_root = Path(cloud_artifact_root) if cloud_artifact_root else None
        self._aws_profile = aws_profile
        self._gcloud = gcloud_configuration
        self._adc_email = gcp_adc_email
        self._terraform = terraform_bin
        self._helm = helm_bin
        self._aws_template = aws_template_dir
        self._gcp_template = gcp_template_dir
        self._env_store = env_store

    # -- entry point -----------------------------------------------------------------------

    def publish(self, migration_id: str) -> dict:
        try:
            return self._publish(migration_id)
        except PublishFailure as failure:
            return {"status": "FAILED", "code": failure.code, "issues": failure.issues}

    def _publish(self, migration_id: str) -> dict:
        with self._sessions() as session:
            registration = self._registration(session, migration_id)

            def build(plan):
                spec, pending = self._validated(registration)
                if registration.target.provider == "ON_PREM":
                    env = self._env(registration, spec, registration.target.namespace)
                    manifest, preview = self._onprem(session, registration, spec, plan, env)
                    return manifest, _with_env(preview, env), [*pending, *_pending(env)]
                manifest, preview = self._cloud(session, registration, plan)
                return manifest, preview, pending

            return self._publish_revision(
                session,
                plan_id_for(migration_id),
                input_digest(registration),
                template_version(registration),
                build,
            )

    @staticmethod
    def _registration(session: Session, migration_id: str) -> PlanningInputRegistration:
        registration = load_planning_input(session, migration_id)
        if registration is None:
            raise PublishFailure("PLANNING_INPUT_MISSING")
        return registration

    def _publish_revision(self, session, plan_id, digest, template, build) -> dict:
        """P3: reuse the latest Revision for the same input Digest, else publish latest+1.

        Ticket 60: a REJECTed Revision is never reused (it can never be approved again), so the
        same input after a REJECT publishes latest+1 for a new decision."""
        latest = latest_revision(session, plan_id)
        if latest is not None:
            published = session.get(PlanPublication, (plan_id, latest))
            if (
                published is not None
                and published.input_digest == digest
                and not _rejected(session, plan_id, latest)
            ):
                return self._result(session, plan_id, latest, "PLAN_REUSED")
        revision = (latest or 0) + 1
        plan = {"plan_id": plan_id, "revision": revision, "template_version": template}
        manifest, preview, pending = build(plan)
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

    # -- ticket 60: what the web shows for a published Revision ------------------------------

    def plan_view(self, migration_id: str, result: dict) -> PlanView:
        """Stored Preview, target, security rules and analysis mapping rows (names only)."""
        with self._sessions() as session:
            row = session.get(PlanRevision, (result["plan_id"], result["revision"]))
            registration = self._registration(session, migration_id)
        manifest = PlanManifest.model_validate(row.manifest)
        identity = manifest.target
        target = {
            "provider": identity.provider,
            "platform": PLATFORMS[identity.provider],
            "location": getattr(identity, "region", None) or identity.environment_id,
        }
        try:
            spec, _ = self._validated(registration)
            rules = _security_rules(spec)
        except PublishFailure:
            rules = ()
        report = self._report(registration)
        return PlanView(
            preview=row.preview,
            target=target,
            security_rules=rules,
            rows=tuple(report.mapping) if report is not None else None,
            mapping_risks=tuple(
                risk
                for risk in (report.unverified_risks if report is not None else ())
                if risk.startswith("MAPPING_NOT_RUN")
            ),
        )

    def _report(self, registration: PlanningInputRegistration) -> AnalysisReport | None:
        """The analysis report the Sandbox verified (shared ArtifactStore); None: unreadable."""
        sha = registration.planning_request.sandbox.report_ref.sha256
        try:
            return AnalysisReport.model_validate_json(self._store.read_raw(sha))
        except (RolloutError, ValueError):
            return None

    def _env(self, registration, spec, namespace: str) -> EnvRendering | None:
        """ENV-04: the approved EnvPlan → app env bindings + Preview lines (names only).

        Reads key names, never values. Decisions (UNREPRESENTABLE, MISSING, ...) become
        `pending_decisions`, so the Revision is published for review and 07 refuses approval."""
        plan = registration.env_plan
        if plan is None:
            return None
        if self._env_store is None:
            raise PublishFailure("ENV_FILE_STORE_UNCONFIGURED")
        database, app = _topology(spec)
        db_release, app_release = _release(database.id), _release(app.id)
        outputs = database_outputs(
            release=db_release,
            namespace=namespace,
            name=database.database.name,
            username=database.database.username,
            password_secret_ref=SecretRefV2(name=f"{db_release}-auth", key="password"),
        )
        try:
            rendered = preview_env(
                self._env_store,
                plan,
                spec.environment.env_file_ref,
                DbOutputRefs.from_outputs(outputs, database_release=db_release),
                app_release=app_release,
            )
        except EnvFileError as error:
            raise PublishFailure("ENV_FILE_UNAVAILABLE", [error.code]) from None
        entries = {entry.key: entry for entry in plan.entries}
        for name, binding in app.env_bindings.items():
            entry = entries.get(name)
            if entry is None or (entry.disposition, entry.binding) != ("REPLACED", binding.output):
                raise PublishFailure("PLANNING_INPUT_INVALID", ["ENV_PLAN_BINDING_MISMATCH"])
        pending = [
            ValidationIssue(
                code=decision.code,
                path=f"environment.env_plan.{decision.key}",
                message="Resolve this .env key before approval.",
            )
            for decision in rendered.decisions
        ]
        bindings = (
            None
            if rendered.decisions
            else {
                item.name: _env_binding(entries[item.name], item, database.id)
                for item in rendered.env
            }
        )
        platform = {app_env_secret_name(app_release), connection_secret_name(db_release)}
        return EnvRendering(
            bindings,
            pending,
            _env_changes(plan, app_release, db_release, rendered),
            tuple(sorted(platform)) if bindings is not None else (),
        )

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

    def _onprem(self, session, registration, spec, plan, env=None) -> tuple[PlanManifest, Preview]:
        target: OnPremTargetSettings = registration.target
        agent = session.get(RegisteredAgent, target.agent_id)
        if agent is None:
            raise PublishFailure("AGENT_NOT_REGISTERED")
        if agent.revoked_at is not None:
            raise PublishFailure("AGENT_REVOKED")
        if agent.cluster_uid != target.cluster_uid:
            raise PublishFailure("CLUSTER_UID_MISMATCH")
        deployment = deployment_request(spec, target, env_bindings=env and env.bindings)
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
        network = network_policies(spec, deployment)
        spec_document = {
            "spec": spec.model_dump(mode="json"),
            "target": target.model_dump(mode="json"),
            "deployment": deployment,
            **_env_plan_document(registration),
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
            "network_policies": ("MANIFEST", network.content),
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
            preview = onprem_preview(
                manifest,
                {role: c for role, (_, c) in contents.items()},
                platform_secrets=env.platform_secrets if env else (),
            )
        except ProvisioningInputError as error:
            raise PublishFailure("PREVIEW_FAILED", _codes(error)) from None
        return manifest, network.preview(preview)

    # -- AWS / GCP -------------------------------------------------------------------------

    def _cloud(self, session, registration, plan) -> tuple[PlanManifest, Preview]:
        if self._cloud_root is None:
            raise PublishFailure("PLAN_FAILED", ["ARTIFACT_ROOT_NOT_CONFIGURED"])
        try:
            request = cloud_plan_input(registration, plan)
        except PlanInputError as error:
            raise PublishFailure("PLAN_FAILED", [error.code]) from None
        if registration.target.provider == "AWS":
            _check_target_connection(session, request)
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
                platform_egress_cidrs=self._egress,  # ticket 47 (D-10)
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

    # -- AWS / GCP workloads after the infrastructure run (ticket 22) ----------------------

    def publish_workloads(self, migration_id: str, infrastructure: dict) -> dict:
        """W1~W6: the WORKLOADS Plan for the cluster the given infrastructure Revision built."""
        try:
            return self._publish_workloads(migration_id, infrastructure)
        except PublishFailure as failure:
            return {"status": "FAILED", "code": failure.code, "issues": failure.issues}

    def _publish_workloads(self, migration_id: str, infrastructure: dict) -> dict:
        with self._sessions() as session:
            registration = self._registration(session, migration_id)
            target = registration.target
            if target.provider == "ON_PREM":
                raise PublishFailure("WORKLOADS_NOT_APPLICABLE")
            if target.workloads is None:
                raise PublishFailure("WORKLOAD_SETTINGS_MISSING")
            infra, refs, cluster = _infrastructure_result(
                session, migration_id, target.provider, infrastructure
            )
            digest = sha256_json(
                {
                    "registration": input_digest(registration),
                    "workloads": target.workloads.model_dump(mode="json"),
                    "infrastructure": {
                        "plan_id": infra.plan.plan_id,
                        "revision": infra.plan.revision,
                        "manifest_digest": manifest_digest(infra),
                        "resource_refs": refs,
                    },
                    "template_version": WORKLOAD_TEMPLATE,
                    "network_policy": NETWORK_POLICY_TEMPLATE,  # ticket 26
                    **env_plan_digest(registration),
                }
            )
            plan_id = workload_plan_id_for(migration_id)

            def build(plan):
                spec, pending = self._validated(registration)
                env = self._env(registration, spec, target.workloads.namespace)
                manifest, preview = self._workloads(
                    session, registration, spec, plan, infra, refs, cluster, env
                )
                return manifest, _with_env(preview, env), [*pending, *_pending(env)]

            return self._publish_revision(session, plan_id, digest, WORKLOAD_TEMPLATE, build)

    def _workloads(self, session, registration, spec, plan, infra, refs, cluster, env=None):
        if self._cloud_root is None:
            raise PublishFailure("PLAN_FAILED", ["ARTIFACT_ROOT_NOT_CONFIGURED"])
        provider, settings = registration.target.provider, registration.target.workloads
        deployment = deployment_request(spec, settings, provider, env and env.bindings)
        try:
            rendered = render_shared_deployment_v2(
                deployment,
                database_chart_dir=DB_CHART,
                application_chart_dir=APP_CHART,
                helm_bin=self._helm,
            )
            values, bindings = _application_template(spec, deployment)
            db = next(r for r in rendered.resources if r.kind == "POSTGRESQL")
            resolved = {
                "DATABASE_HOST": db.outputs.host,
                "DATABASE_SECRET_NAME": db.outputs.password_secret_ref.name,
                "DATABASE_URL_SECRET_NAME": connection_url_secret_name(db.release_name),
            }
            resolved = {rule["source"]: resolved[rule["source"]] for rule in bindings}
            # The bytes 09/11 resolve at execution must render with the approved Chart.
            render_application(
                resolve_application(values, resolved), chart_dir=APP_CHART, helm_bin=self._helm
            )
            db_chart = package_chart(DB_CHART, helm_bin=self._helm)
            app_chart = package_chart(APP_CHART, helm_bin=self._helm)
        except (ProvisioningInputError, ApplicationRenderError) as error:
            codes = _codes(error) if isinstance(error, ProvisioningInputError) else []
            raise PublishFailure("RENDER_FAILED", codes or ["RENDER_FAILED"]) from None
        network = network_policies(spec, deployment)
        spec_document = {
            "spec": spec.model_dump(mode="json"),
            "target": registration.target.model_dump(mode="json"),
            "infrastructure": {
                "plan_id": infra.plan.plan_id,
                "revision": infra.plan.revision,
                "manifest_digest": manifest_digest(infra),
                "resource_refs": refs,
            },
            "deployment": deployment,
            **_env_plan_document(registration),
        }
        contents = {
            "spec": canonical_json_bytes(spec_document),
            "database_chart": db_chart,
            "database_values": canonical_json_bytes(db.helm_values.model_dump(mode="json")),
            "application_chart": app_chart,
            "application_values": canonical_json_bytes(values),
            "bindings": canonical_json_bytes(bindings),
            "network_policies": network.content,
        }
        prefix = f"{plan['plan_id']}-r{plan['revision']}"
        _write_artifacts(self._cloud_root / prefix, contents.values())
        artifacts = {
            role: ArtifactRef(
                id=f"{prefix}-{role}",
                version="1",
                kind=ARTIFACT_KINDS[role],
                sha256=sha256_bytes(content),
                size_bytes=len(content),
            )
            for role, content in contents.items()
        }
        manifest = PlanManifest(
            contract_version="1",
            digest_format="cmp-json-v1",
            migration_id=spec.migration_id,
            plan=plan,
            target=infra.target,  # identical identity: 09/11 resolve this infrastructure run
            scope="WORKLOADS",
            image_uri=values["image"]["uri"],
            artifacts=artifacts,
        )
        deployed = session.scalar(
            select(PlanExecution.plan_id).where(
                PlanExecution.plan_id == plan["plan_id"], PlanExecution.status == "SUCCEEDED"
            )
        )
        try:
            preview = cloud_workload_preview(
                manifest, contents, cluster_name=cluster.cluster_name, deployed=bool(deployed)
            )
        except ProvisioningInputError as error:
            raise PublishFailure("PREVIEW_FAILED", _codes(error)) from None
        return manifest, network.preview(preview)


@dataclass(frozen=True)
class NetworkRendering:
    """Ticket 26: the approved NetworkPolicy artifact bytes and their Preview lines."""

    content: bytes
    changes: tuple[ResourceChange, ...]
    defaults: tuple[ResourceChange, ...]

    def preview(self, preview: Preview) -> Preview:
        return preview.model_copy(
            update={
                "changes": [*preview.changes, *self.changes],
                "applied_defaults": [*preview.applied_defaults, *self.defaults],
            }
        )


def network_policies(spec: ProvisioningSpecV2, deployment: dict) -> NetworkRendering:
    """Approved `network_intent` (+ the app → DB binding) → production NetworkPolicies."""
    database, app = deployment["resources"]
    releases = {r["id"]: r["release_name"] for r in deployment["resources"]}
    intents = with_connections(
        [i.model_dump(mode="json") for i in spec.requirements.network_intent],
        [(app["id"], database["id"])],
    )
    shape = {
        "provider": deployment["provider"],
        "releases": releases,
        "applications": {app["release_name"]: app["port"]},
        "databases": [database["release_name"]],
        "intents": intents,
        # App telemetry (OTLP to the platform edge); empty = no extra egress.
        "telemetry_cidr": os.environ.get("CMP_TELEMETRY_EGRESS_CIDR", "").strip() or None,
    }
    policies = production_policies(namespace=deployment["namespace"], **shape)
    lines, defaults = describe(**shape)

    def change(text: str) -> ResourceChange:
        return ResourceChange(
            resource_type="NETWORK", action="CREATE", name=f"NetworkPolicy {text}"
        )

    return NetworkRendering(
        canonical_json_bytes({"policies": policies}),
        tuple(change(line) for line in lines),
        tuple(change(line) for line in defaults),
    )


def _rejected(session: Session, plan_id: str, revision: int) -> bool:
    decision = session.scalar(
        select(PlanApproval.decision).where(
            PlanApproval.plan_id == plan_id, PlanApproval.revision == revision
        )
    )
    return decision == "REJECT"


def _security_rules(spec: ProvisioningSpecV2) -> tuple[dict, ...]:
    """Approved network intent (+ app → DB) as allow rules, then the default deny (ticket 26)."""
    database, app = _topology(spec)
    intents = with_connections(
        [i.model_dump(mode="json") for i in spec.requirements.network_intent],
        [(app.id, database.id)],
    )
    rules = [
        {"from": "ingress", "to": app.id, "port": app.port, "protocol": "TCP", "action": "ALLOW"}
    ]
    rules += [
        {
            "from": intent["source_resource_id"],
            "to": intent.get("destination_resource_id") or intent["external_host"],
            "port": intent["port"],
            "protocol": intent.get("protocol") or "TCP",
            "action": "ALLOW",
        }
        for intent in intents
    ]
    rules.append({"from": "*", "to": "*", "port": None, "protocol": "ANY", "action": "DENY"})
    return tuple(rules)


def _env_plan_document(registration: PlanningInputRegistration) -> dict:
    """ENV-04: the EnvPlan rides in the approved SPEC artifact (Digest in the manifest), where
    the deploy Activity and the Agent secrets API read it. Absent → the old bytes exactly."""
    if registration.env_plan is None:
        return {}
    return {"env_plan": registration.env_plan.model_dump(mode="json")}


def _with_env(preview: Preview, env: EnvRendering | None) -> Preview:
    if env is None or not env.defaults:
        return preview
    return preview.model_copy(
        update={"applied_defaults": [*preview.applied_defaults, *env.defaults]}
    )


def _pending(env: EnvRendering | None) -> list[ValidationIssue]:
    return env.pending if env is not None else []


def _infrastructure_result(session: Session, migration_id: str, provider: str, ref: dict):
    """W4: the stored, latest, fully SUCCEEDED infrastructure Revision of this Migration."""
    plan_id, revision = ref.get("plan_id"), ref.get("revision")
    row = session.get(PlanRevision, (plan_id, revision)) if plan_id and revision else None
    if row is None or row.migration_id != migration_id:
        raise PublishFailure("INFRASTRUCTURE_NOT_FOUND")
    manifest = PlanManifest.model_validate(row.manifest)
    if manifest.scope != "INFRASTRUCTURE" or manifest.target.provider != provider:
        raise PublishFailure("INFRASTRUCTURE_NOT_FOUND")
    if latest_revision(session, plan_id) != revision:
        raise PublishFailure("INFRASTRUCTURE_STALE")
    executions = session.scalars(
        select(PlanExecution).where(
            PlanExecution.plan_id == plan_id, PlanExecution.revision == revision
        )
    ).all()
    if not executions or any(e.status != "SUCCEEDED" for e in executions):
        raise PublishFailure("INFRASTRUCTURE_NOT_SUCCEEDED")
    refs: dict[str, str] = {}
    for execution in executions:
        refs.update(execution.resource_refs or {})
    cluster = CLUSTER_TARGETS[provider](refs, manifest.target.region)
    if cluster is None:
        raise PublishFailure("INFRASTRUCTURE_OUTPUTS_MISSING")
    return manifest, dict(sorted(refs.items())), cluster


def _application_template(spec: ProvisioningSpecV2, deployment: dict) -> tuple[dict, list]:
    """W6: the 09/11 v1 App Values template and its DynamicBinding rules."""
    database, app = deployment["resources"]
    env = {name: _v1_source(binding) for name, binding in app["env_bindings"].items()}
    template = {
        "contract_version": "1",
        "environment": "PRODUCTION",
        "provider": deployment["provider"],
        "release_name": app["release_name"],
        "namespace": deployment["namespace"],
        "config_ref": spec.environment.env_file_ref.model_dump(mode="json"),
        "image": app["image"],
        "application": {
            "cpu_millicores": app["resources"]["cpu_millicores"],
            "memory_mib": app["resources"]["memory_mib"],
            "replicas": app["replicas"],
            "port": app["port"],
            "health_path": app["health_path"],
        },
        "database": {
            "host": None,
            "port": 5432,
            "name": database["database"]["name"],
            "username": database["database"]["username"],
            "secret_name": None,
            "secret_key": database["database"]["password_secret_ref"]["key"],
        },
        "required_env": sorted(env),
        "env": [{"name": name, "source": env[name]} for name in sorted(env)],
    }
    plan = prepare_application(template)
    return plan.values.model_dump(mode="json"), [r.model_dump(mode="json") for r in plan.bindings]


def _v1_source(binding: dict) -> dict:
    """v2 binding → 09/11 v1 env source. A Secret reference (ENV-04 `-env`/`-conn`) is a
    `USER_SECRET` the deploy Activity creates before its existence check."""
    if "secret_ref" in binding:
        return {"kind": "USER_SECRET", **binding["secret_ref"]}
    return {"kind": "DATABASE", "field": DATABASE_FIELDS[binding["output"]]}


def _write_artifacts(folder: Path, contents) -> None:
    """Content-addressed files where 09/11 read them; never replace different bytes."""
    folder.mkdir(parents=True, exist_ok=True)
    for content in contents:
        path = folder / sha256_bytes(content)
        if path.is_symlink() or (path.exists() and path.read_bytes() != content):
            raise PublishFailure("PLAN_DIRECTORY_CONFLICT")
        if not path.exists():
            path.write_bytes(content)
