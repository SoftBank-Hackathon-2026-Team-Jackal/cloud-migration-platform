"""Ticket 09: deploy approved PostgreSQL + App workloads to a ticket-08 EKS cluster.

`execute_aws_workloads` re-reads the Plan Revision and approval from the business DB, verifies
the stored artifact bytes against the approved ArtifactRef digests, prepares cluster
prerequisites (StorageClass, AWS Load Balancer Controller), creates the DB Secret without
exposing it, installs or upgrades the two Helm Releases idempotently and reports READY only
after every required check passes (decisions W1~W11 in tickets/provisioning/09).

The result holds codes and non-secret references only. Secret values, kubeconfig and command
output never enter the result, logs or Temporal history.
"""

import asyncio
import json
import secrets
import tarfile
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from cmp_api.approvals import PlanApproval, PlanExecution, PlanRevision, latest_revision
from cmp_contracts.application import ApplicationValues
from cmp_contracts.chart import DynamicBinding, PostgresChartValues
from cmp_contracts.plan import PlanManifest
from cmp_providers.application import prepare_application, resolve_application, verify_application
from cmp_providers.artifacts import canonical_json_bytes, verify_artifact_bytes
from cmp_providers.plans import manifest_digest
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity

ACTIVITY_NAME = "execute_aws_workloads"
HEARTBEAT_SECONDS = 30
WORKLOAD_ROLES = (
    "database_chart",
    "database_values",
    "application_chart",
    "application_values",
    "bindings",
)
MANAGED_BY = {"app.kubernetes.io/managed-by": "cmp-provisioning"}
PULL_FAILURES = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}
CONFIG_FAILURES = {"CreateContainerConfigError", "CreateContainerError"}


@dataclass(frozen=True)
class ClusterTarget:
    """Non-secret identifiers of the ticket-08 cluster (resource_refs of the infra run)."""

    cluster_name: str
    region: str
    vpc_id: str
    lb_controller_role_arn: str


@dataclass(frozen=True)
class Timeouts:
    readiness_seconds: float = 600
    alb_seconds: float = 600
    interval_seconds: float = 10


class ClusterSession(Protocol):
    """kubectl/Helm operations on one connected cluster. Errors raise without secret text."""

    def ensure_lb_controller(self, target: ClusterTarget) -> None: ...
    def storage_class(self, name: str) -> dict | None: ...
    def ensure_namespace(self, name: str) -> None: ...
    def apply(self, manifest: dict) -> None: ...
    def secret_keys(self, namespace: str, name: str) -> set[str] | None: ...
    def create_secret(self, namespace: str, name: str, data: dict[str, str]) -> None: ...
    def configmap_keys(self, namespace: str, name: str) -> set[str] | None: ...
    def release(self, namespace: str, name: str) -> dict | None: ...
    def helm_upgrade_install(self, namespace: str, name: str, chart: Path, values: dict): ...
    def pvc_phase_of(self, namespace: str, name: str) -> str | None: ...
    def workload_ready(self, namespace: str, kind: str, name: str) -> bool: ...
    def waiting_reasons(self, namespace: str, instance: str) -> set[str]: ...
    def database_probe(self, namespace: str, pod: str, host: str) -> str: ...
    def service_health(self, namespace: str, service: str, path: str) -> bool: ...
    def ingress_hostname(self, namespace: str, name: str) -> str | None: ...


class WorkloadRuntime(Protocol):
    def account_id(self) -> str: ...
    def connect(self, target: ClusterTarget) -> AbstractContextManager[ClusterSession]: ...
    def http_get_ok(self, url: str) -> bool: ...


class TargetResolver(Protocol):
    def resolve(
        self, session: Session, manifest: PlanManifest, request: dict
    ) -> ClusterTarget | None: ...


# ticket-08 result reference name for each ClusterTarget field (aws_apply.OUTPUT_REFS).
INFRA_REF_KEYS = {
    "cluster_name": "eks_cluster_name",
    "vpc_id": "vpc_id",
    "lb_controller_role_arn": "lb_controller_role_arn",
}


def target_from_refs(refs: dict, region: str) -> ClusterTarget | None:
    values = {field: refs.get(key) for field, key in INFRA_REF_KEYS.items()}
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return ClusterTarget(region=region, **values)


class ExecutionRefsResolver:
    """W11: find the ticket-08 cluster for this Plan's target environment.

    FULL: the Workflow passes the infrastructure step's refs in `request["resource_refs"]`.
    WORKLOADS: use the latest SUCCEEDED INFRASTRUCTURE/FULL execution whose stored manifest has
    the identical target identity (account, region, environment, State).
    """

    def resolve(self, session: Session, manifest: PlanManifest, request: dict):
        region = manifest.target.region
        found = target_from_refs(request.get("resource_refs") or {}, region)
        if found is not None:
            return found
        if not hasattr(PlanExecution, "resource_refs"):  # schema before ticket 08
            return None
        rows = session.execute(
            select(PlanExecution, PlanRevision)
            .join(
                PlanRevision,
                (PlanRevision.plan_id == PlanExecution.plan_id)
                & (PlanRevision.revision == PlanExecution.revision),
            )
            .where(PlanExecution.status == "SUCCEEDED")
        ).all()
        best = None
        for execution, revision in rows:
            other = PlanManifest.model_validate(revision.manifest)
            if other.scope not in {"INFRASTRUCTURE", "FULL"} or other.target != manifest.target:
                continue
            target = target_from_refs(execution.resource_refs or {}, region)
            if target is not None and (best is None or execution.finished_at > best[0]):
                best = (execution.finished_at or 0, target)
        return best[1] if best else None


class WorkloadFailure(Exception):
    def __init__(self, code: str, status: str = "FAILED"):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass
class Approved:
    manifest: PlanManifest
    target: ClusterTarget
    db_values: PostgresChartValues
    app_values: dict
    files: dict[str, bytes]


def _outcome(status: str, code: str, refs: dict[str, str]) -> dict:
    return {"status": status, "code": code, "resource_refs": dict(refs)}


def chart_identity(content: bytes, workdir: Path) -> str:
    """`<name>-<version>` from the packaged Chart.yaml (Helm's own release chart label)."""
    path = workdir / "identity.tgz"
    path.write_bytes(content)
    with tarfile.open(path) as archive:
        member = next(
            m
            for m in archive.getmembers()
            if m.name.count("/") == 1 and m.name.endswith("/Chart.yaml")
        )
        meta = archive.extractfile(member).read().decode()
    fields = {}
    for line in meta.splitlines():
        key, _, value = line.partition(":")
        if key in {"name", "version"}:
            fields[key] = value.strip().strip("'\"")
    return f"{fields['name']}-{fields['version']}"


def storage_class_manifest(name: str) -> dict:
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": name, "labels": MANAGED_BY},
        "provisioner": "ebs.csi.aws.com",
        "parameters": {"type": "gp3", "encrypted": "true"},
        "reclaimPolicy": "Retain",
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": True,
    }


def ingress_manifest(app: dict) -> dict:
    """W5: fixed ALB template derived only from approved App Values. HTTP:80, no TLS (W4)."""
    name, port = app["release_name"], app["application"]["port"]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "namespace": app["namespace"],
            "labels": MANAGED_BY,
            "annotations": {
                "alb.ingress.kubernetes.io/scheme": "internet-facing",
                "alb.ingress.kubernetes.io/target-type": "ip",
                "alb.ingress.kubernetes.io/listen-ports": '[{"HTTP": 80}]',
                "alb.ingress.kubernetes.io/healthcheck-path": app["application"]["health_path"],
            },
        },
        "spec": {
            "ingressClassName": "alb",
            "rules": [
                {
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": name, "port": {"number": port}}},
                            }
                        ]
                    }
                }
            ],
        },
    }


class AwsWorkloadsActivities:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        artifact_root: Path | None,
        resolver: TargetResolver,
        runtime: WorkloadRuntime,
        timeouts: Timeouts | None = None,
    ):
        self._session_factory = session_factory
        self._root = artifact_root
        self._resolver = resolver
        self._runtime = runtime
        self._timeouts = timeouts or Timeouts()

    # ---- approval + artifact verification (no cluster access) ----

    def _load(self, request: dict) -> Approved:
        with self._session_factory() as session:
            approval = session.get(PlanApproval, request["approval_id"])
            if approval is None:
                raise WorkloadFailure("APPROVAL_NOT_FOUND")
            row = session.get(PlanRevision, (request["plan_id"], request["revision"]))
            if row is None:
                raise WorkloadFailure("PLAN_NOT_FOUND")
            if (approval.plan_id, approval.revision) != (row.plan_id, row.revision):
                raise WorkloadFailure("APPROVAL_TARGET_MISMATCH")
            if approval.decision != "APPROVE":
                raise WorkloadFailure("PLAN_NOT_APPROVED")
            if row.revision != latest_revision(session, row.plan_id):
                raise WorkloadFailure("STALE_REVISION")
            manifest = PlanManifest.model_validate(row.manifest)
            digest = manifest_digest(manifest)
            if digest != request["manifest_digest"] or digest != approval.manifest_digest:
                raise WorkloadFailure("MANIFEST_DIGEST_MISMATCH")
            if manifest.target.provider != "AWS" or request["provider"] != "AWS":
                raise WorkloadFailure("PROVIDER_MISMATCH")
            if (
                manifest.scope not in {"WORKLOADS", "FULL"}
                or request["scope"] != manifest.scope
                or approval.scope != manifest.scope
            ):
                raise WorkloadFailure("SCOPE_MISMATCH")
            files = self._read_artifacts(manifest)
            db_values, app_values = self._verify_values(manifest, files)
            target = self._resolver.resolve(session, manifest, request)
        if target is None:
            raise WorkloadFailure("CLUSTER_TARGET_UNAVAILABLE")
        if target.region != manifest.target.region:
            raise WorkloadFailure("REGION_MISMATCH")
        return Approved(manifest, target, db_values, app_values, files)

    def _read_artifacts(self, manifest: PlanManifest) -> dict[str, bytes]:
        if self._root is None:
            raise WorkloadFailure("ARTIFACT_ROOT_UNCONFIGURED")
        folder = self._root / f"{manifest.plan.plan_id}-r{manifest.plan.revision}"
        files = {}
        for role in WORKLOAD_ROLES:
            ref = manifest.artifacts.get(role)
            if ref is None:
                raise WorkloadFailure("ARTIFACT_MISSING")
            try:
                content = (folder / ref.sha256).read_bytes()
            except OSError:
                raise WorkloadFailure("ARTIFACT_MISSING") from None
            try:
                verify_artifact_bytes(ref, content)
            except ValueError:
                raise WorkloadFailure("ARTIFACT_DIGEST_MISMATCH") from None
            files[role] = content
        return files

    def _verify_values(self, manifest: PlanManifest, files: dict[str, bytes]):
        try:
            db = PostgresChartValues.model_validate_json(files["database_values"])
            template = json.loads(files["application_values"])
            rules = TypeAdapter(list[DynamicBinding]).validate_json(files["bindings"])
        except ValueError:
            raise WorkloadFailure("INVALID_VALUES") from None
        if (db.environment, db.provider) != ("PRODUCTION", "AWS"):
            raise WorkloadFailure("INVALID_VALUES")
        try:
            plan = prepare_application(template)
        except ValueError:
            raise WorkloadFailure("INVALID_APP_VALUES") from None
        if canonical_json_bytes(plan.values.model_dump(mode="json")) != files["application_values"]:
            raise WorkloadFailure("INVALID_APP_VALUES")
        if [r.model_dump() for r in rules] != [r.model_dump() for r in plan.bindings]:
            raise WorkloadFailure("BINDINGS_MISMATCH")
        app = plan.values
        if (app.environment, app.provider) != ("PRODUCTION", "AWS"):
            raise WorkloadFailure("INVALID_APP_VALUES")
        if app.namespace != db.namespace:
            raise WorkloadFailure("NAMESPACE_MISMATCH")
        if app.image.uri != manifest.image_uri:
            raise WorkloadFailure("APP_IMAGE_MISMATCH")
        connection = app.database
        if (connection.port, connection.name, connection.username, connection.secret_key) != (
            5432,
            db.database.name,
            db.database.username,
            db.database.secret_key,
        ):
            raise WorkloadFailure("DB_BINDING_MISMATCH")
        resolved = {
            "DATABASE_HOST": f"{db.release_name}.{db.namespace}.svc",
            "DATABASE_SECRET_NAME": db.database.secret_name,
        }
        resolved = {r.source: resolved[r.source] for r in plan.bindings}
        try:
            values = resolve_application(template, resolved)
            verify_application(template, values, resolved)
        except ValueError:
            raise WorkloadFailure("INVALID_APP_VALUES") from None
        return db, values

    # ---- cluster execution ----

    async def _call(self, fn, *args):
        """Run a blocking port call in a thread, heartbeating so long CLI calls stay alive."""
        work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        while True:
            done, _ = await asyncio.wait({work}, timeout=HEARTBEAT_SECONDS)
            if done:
                return work.result()
            activity.heartbeat("running")

    async def _wait(self, check, seconds: float, phase: str) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            activity.heartbeat(phase)
            if await asyncio.to_thread(check):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self._timeouts.interval_seconds)

    async def _release(self, cluster, workdir: Path, namespace, name, chart: bytes, values):
        identity = chart_identity(chart, workdir)
        current = await self._call(cluster.release, namespace, name)
        if current is not None:
            status = current["status"]
            if status.startswith("pending"):
                raise WorkloadFailure("HELM_RELEASE_PENDING", "UNKNOWN")
            if status != "deployed":
                raise WorkloadFailure("HELM_RELEASE_FAILED")
            if current["chart"] == identity and canonical_json_bytes(
                current["values"]
            ) == canonical_json_bytes(values):
                return
        path = workdir / f"{name}.tgz"
        path.write_bytes(chart)
        activity.heartbeat(f"helm:{name}")
        try:
            await self._call(cluster.helm_upgrade_install, namespace, name, path, values)
        except Exception:  # noqa: BLE001 - the release may be partially applied
            raise WorkloadFailure("HELM_RESULT_UNKNOWN", "UNKNOWN") from None

    async def _pods_ready(self, cluster, namespace, kind, name) -> None:
        def check():
            reasons = cluster.waiting_reasons(namespace, name)
            if reasons & PULL_FAILURES:
                raise WorkloadFailure("IMAGE_PULL_FAILED")
            if reasons & CONFIG_FAILURES:
                raise WorkloadFailure("SECRET_REFERENCE_INVALID")
            return cluster.workload_ready(namespace, kind, name)

        if not await self._wait(check, self._timeouts.readiness_seconds, f"ready:{name}"):
            reasons = await self._call(cluster.waiting_reasons, namespace, name)
            raise WorkloadFailure(
                "POD_CRASHING" if "CrashLoopBackOff" in reasons else "POD_NOT_READY"
            )

    async def _deploy(self, cluster, approved: Approved, refs: dict[str, str]) -> None:
        db, app = approved.db_values, approved.app_values
        namespace = db.namespace
        try:
            await self._call(cluster.ensure_lb_controller, approved.target)
        except Exception:  # noqa: BLE001 - CLI output is never surfaced
            raise WorkloadFailure("LB_CONTROLLER_NOT_READY") from None
        refs["lb_controller"] = "kube-system/aws-load-balancer-controller"

        storage = await self._call(cluster.storage_class, db.storage.class_name)
        if storage is None:
            await self._call(cluster.apply, storage_class_manifest(db.storage.class_name))
        elif storage.get("provisioner") != "ebs.csi.aws.com":
            raise WorkloadFailure("STORAGE_CLASS_MISMATCH")
        refs["storage_class"] = db.storage.class_name

        await self._call(cluster.ensure_namespace, namespace)
        refs["namespace"] = namespace

        # W7: user-provided references must already exist; this run never creates them.
        for item in ApplicationValues.model_validate(app).env:
            source = item.source
            if source.kind == "USER_SECRET":
                keys = await self._call(cluster.secret_keys, namespace, source.name)
                if keys is None or source.key not in keys:
                    raise WorkloadFailure("USER_SECRET_MISSING")
            elif source.kind == "CONFIG_MAP":
                keys = await self._call(cluster.configmap_keys, namespace, source.name)
                if keys is None or source.key not in keys:
                    raise WorkloadFailure("CONFIG_MAP_MISSING")

        secret_name, secret_key = db.database.secret_name, db.database.secret_key
        keys = await self._call(cluster.secret_keys, namespace, secret_name)
        if keys is None:
            # Generated here, sent only via stdin by the real runtime, never returned.
            data = {secret_key: secrets.token_urlsafe(32)}
            await self._call(cluster.create_secret, namespace, secret_name, data)
            del data
        elif secret_key not in keys:
            raise WorkloadFailure("DB_SECRET_INVALID")
        refs["db_secret_ref"] = f"{secret_name}/{secret_key}"

        with TemporaryDirectory(prefix="cmp-workloads-") as tmp:
            workdir = Path(tmp)
            db_payload = json.loads(approved.files["database_values"])  # approved bytes
            await self._release(
                cluster,
                workdir,
                namespace,
                db.release_name,
                approved.files["database_chart"],
                db_payload,
            )
            refs["db_release"] = db.release_name

            pvc = db.release_name
            if not await self._wait(
                lambda: cluster.pvc_phase_of(namespace, pvc) == "Bound",
                self._timeouts.readiness_seconds,
                "pvc",
            ):
                raise WorkloadFailure("PVC_NOT_BOUND")
            refs["db_pvc"] = pvc
            await self._pods_ready(cluster, namespace, "statefulset", db.release_name)

            host = f"{db.release_name}.{namespace}.svc"
            try:
                probe = await self._call(
                    cluster.database_probe, namespace, f"{db.release_name}-0", host
                )
            except Exception:  # noqa: BLE001 - psql output is never surfaced
                raise WorkloadFailure("DB_CONNECT_FAILED") from None
            version, _, replication = probe.strip().partition("|")
            if not version.startswith("16") or len(version) != 6:
                raise WorkloadFailure("DB_VERSION_MISMATCH")
            refs.update(
                db_host=host,
                db_port="5432",
                db_name=db.database.name,
                db_user=db.database.username,
                db_replication_capable="true" if replication == "true" else "false",
            )

            name = app["release_name"]
            await self._release(
                cluster, workdir, namespace, name, approved.files["application_chart"], app
            )
            refs["app_release"] = name
            refs["app_image"] = app["image"]["uri"]
        await self._pods_ready(cluster, namespace, "deployment", name)

        health = app["application"]["health_path"]
        if not await self._call(cluster.service_health, namespace, name, health):
            raise WorkloadFailure("HEALTH_CHECK_FAILED")
        refs["app_internal_endpoint"] = (
            f"http://{name}.{namespace}.svc:{app['application']['port']}"
        )

        await self._call(cluster.apply, ingress_manifest(app))
        refs["ingress"] = f"{namespace}/{name}"
        hostname: list[str] = []

        def address():
            value = cluster.ingress_hostname(namespace, name)
            if value:
                hostname.append(value)
            return bool(value)

        if not await self._wait(address, self._timeouts.alb_seconds, "alb-address"):
            raise WorkloadFailure("ALB_ADDRESS_PENDING")
        refs["alb_hostname"] = hostname[-1]
        endpoint = f"http://{hostname[-1]}"
        if not await self._wait(
            lambda: self._runtime.http_get_ok(endpoint + health),
            self._timeouts.alb_seconds,
            "alb-health",
        ):
            raise WorkloadFailure("ALB_UNREACHABLE")
        refs["app_external_endpoint"] = endpoint

    async def _execute(self, request: dict) -> dict:
        refs: dict[str, str] = {}
        try:
            approved = await self._call(self._load, request)
            try:
                account = await self._call(self._runtime.account_id)
            except Exception:  # noqa: BLE001
                raise WorkloadFailure("AWS_IDENTITY_UNAVAILABLE") from None
            if account != approved.manifest.target.account_id:
                raise WorkloadFailure("ACCOUNT_MISMATCH")
            refs["cluster_name"] = approved.target.cluster_name
            activity.heartbeat("connect")
            connection = self._runtime.connect(approved.target)
            try:
                cluster = await self._call(connection.__enter__)
            except Exception:  # noqa: BLE001 - no kubeconfig or CLI output in results
                raise WorkloadFailure("CLUSTER_ACCESS_FAILED") from None
            try:
                await self._deploy(cluster, approved, refs)
            finally:
                await self._call(connection.__exit__, None, None, None)
        except WorkloadFailure as failure:
            return _outcome(failure.status, failure.code, refs)
        except Exception:  # noqa: BLE001 - boundary: codes only, never raw errors
            activity.logger.warning("execute_aws_workloads interrupted", extra={"refs": refs})
            return _outcome("UNKNOWN", "EXECUTION_INTERRUPTED", refs)
        return _outcome("SUCCEEDED", "WORKLOADS_READY", refs)

    @activity.defn(name=ACTIVITY_NAME)
    async def execute_aws_workloads(self, request: dict) -> dict:
        return await self._execute(request)
