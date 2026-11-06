"""Ticket 53: tear down an AWS/GCP target that a Plan Revision created (operator-started, D1).

Three Activities, called in this order by `CloudTeardownWorkflow`:

1. `teardown_cloud_workloads` — `helm uninstall` of every approved WORKLOADS/FULL Release for the
   same target (app, then DB), delete every Ingress and `LoadBalancer` Service in the cluster and
   wait until they are gone (their controllers delete the ALB / GCE LB / NEGs first), then apply
   the PVC policy: keep and report Retain disks, or with `delete_data` let the CSI driver delete
   the platform Namespaces' disks (D3, D4). Idempotent; safe to retry.
2. `plan_cloud_destroy` — the reviewed `terraform plan -destroy` (D5, D6).
3. `apply_cloud_destroy` — destroy only the confirmed review Digest.

The target is derived only from an executed INFRASTRUCTURE/FULL Plan Revision; the request cannot
name a cluster, bucket or Namespace. Results carry codes, counts and non-secret IDs only.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cmp_api.approvals import EXECUTION_STEP, PlanExecution, PlanRevision
from cmp_contracts.plan import PlanManifest
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.artifacts import verify_artifact_bytes
from cmp_providers.aws_apply import Runner, subprocess_runner
from cmp_providers.aws_credentials import resolve_aws_profiles
from cmp_providers.aws_preview import AwsPlanInput
from cmp_providers.cloud_destroy import apply_destroy, plan_destroy
from cmp_providers.gcp_apply import AdcLookup, default_adc_email
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity

from cmp_worker.activities import aws_workloads, gcp_workloads
from cmp_worker.activities.terraform_run import run_with_heartbeat

DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}
INFRA_SCOPES = {"INFRASTRUCTURE", "FULL"}
WORKLOAD_SCOPES = {"WORKLOADS", "FULL"}


@dataclass(frozen=True)
class TeardownTimeouts:
    load_balancer_seconds: float = 900
    volume_seconds: float = 600
    interval_seconds: float = 10


class TeardownCluster(Protocol):
    def helm_uninstall(self, namespace: str, name: str) -> None: ...
    def load_balancer_objects(self) -> list[tuple[str, str, str]]: ...  # (kind, ns, name)
    def delete_object(self, kind: str, namespace: str | None, name: str) -> None: ...
    def claims(self, namespace: str) -> list[dict]: ...  # {name, volume}
    def persistent_volumes(self) -> list[dict]: ...  # {name, reclaim, handle, namespace}
    def set_reclaim_policy(self, volume: str, policy: str) -> None: ...


class TeardownFailure(Exception):
    def __init__(self, code: str, status: str = "FAILED"):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass
class TeardownContext:
    manifest: PlanManifest
    artifact_dir: Path
    spec: dict
    cluster: Any | None  # aws_workloads.ClusterTarget / gcp_workloads.GkeTarget
    releases: list[tuple[str, str, str]] = field(default_factory=list)  # (ns, app, db)


def outcome(status: str, code: str, refs: dict[str, str]) -> dict:
    return {"status": status, "code": code, "resource_refs": dict(refs)}


def _read_role(root: Path, manifest: PlanManifest, role: str) -> dict:
    ref = manifest.artifacts.get(role)
    if ref is None:
        raise TeardownFailure("ARTIFACT_MISSING")
    folder = root / f"{manifest.plan.plan_id}-r{manifest.plan.revision}"
    try:
        content = (folder / ref.sha256).read_bytes()
        verify_artifact_bytes(ref, content)
        return json.loads(content)
    except (OSError, ValueError):
        raise TeardownFailure("ARTIFACT_DIGEST_MISMATCH") from None


def _names(values: dict) -> tuple[str, str]:
    namespace, release = values.get("namespace"), values.get("release_name")
    if not all(isinstance(v, str) and DNS_LABEL.match(v) for v in (namespace, release)):
        raise TeardownFailure("INVALID_VALUES")
    return namespace, release


def load_context(session: Session, root: Path | None, request: dict) -> TeardownContext:
    """D1: everything comes from the executed Revision and the approved Plans of its target."""
    if root is None:
        raise TeardownFailure("ARTIFACT_ROOT_NOT_CONFIGURED")
    row = session.get(PlanRevision, (request["plan_id"], request["revision"]))
    if row is None:
        raise TeardownFailure("PLAN_NOT_FOUND")
    manifest = PlanManifest.model_validate(row.manifest)
    if manifest.scope not in INFRA_SCOPES or manifest.target.provider not in ("AWS", "GCP"):
        raise TeardownFailure("UNSUPPORTED_EXECUTION_SCOPE")
    execution = session.get(PlanExecution, (row.plan_id, row.revision, EXECUTION_STEP))
    if execution is None:
        raise TeardownFailure("NOT_EXECUTED")
    if execution.status == "RUNNING":
        raise TeardownFailure("EXECUTION_IN_PROGRESS")
    artifact_dir = root / f"{row.plan_id}-r{row.revision}"
    try:
        spec = json.loads((artifact_dir / "spec.json").read_bytes())
    except (OSError, ValueError):
        raise TeardownFailure("ARTIFACT_NOT_FOUND") from None
    module = aws_workloads if manifest.target.provider == "AWS" else gcp_workloads
    cluster = module.target_from_refs(execution.resource_refs or {}, manifest.target.region)

    releases: list[tuple[str, str, str]] = []
    rows = session.execute(
        select(PlanRevision).join(
            PlanExecution,
            (PlanExecution.plan_id == PlanRevision.plan_id)
            & (PlanExecution.revision == PlanRevision.revision),
        )
    ).scalars()
    for other in rows:
        plan = PlanManifest.model_validate(other.manifest)
        if plan.scope not in WORKLOAD_SCOPES or plan.target != manifest.target:
            continue
        if "database_values" not in plan.artifacts:  # FULL Plans without workload artifacts
            continue
        db_ns, db_release = _names(_read_role(root, plan, "database_values"))
        app_ns, app_release = _names(_read_role(root, plan, "application_values"))
        if db_ns != app_ns:
            raise TeardownFailure("NAMESPACE_MISMATCH")
        entry = (db_ns, app_release, db_release)
        if entry not in releases:
            releases.append(entry)
    return TeardownContext(manifest, artifact_dir, spec, cluster, releases)


class CloudTeardownActivities:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        artifact_root: Path | str | None,
        aws_runtime=None,
        gcp_runtime=None,
        aws_profile: str | None = None,
        gcloud_configuration: str | None = None,
        adc_email: str | None = None,
        terraform_bin: str = "terraform",
        runner: Runner = subprocess_runner,
        adc_lookup: AdcLookup = default_adc_email,
        timeouts: TeardownTimeouts | None = None,
    ):
        self._session_factory = session_factory
        self._root = Path(artifact_root) if artifact_root else None
        self._runtimes = {"AWS": aws_runtime, "GCP": gcp_runtime}
        self._aws_profile = aws_profile
        self._gcloud_configuration = gcloud_configuration
        self._adc_email = adc_email
        self._terraform_bin = terraform_bin
        self._runner = runner
        self._adc_lookup = adc_lookup
        self._timeouts = timeouts or TeardownTimeouts()

    # ---- shared ----

    def _context(self, request: dict) -> TeardownContext:
        with self._session_factory() as session:
            return load_context(session, self._root, request)

    @staticmethod
    async def _call(fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def _wait(self, check, seconds: float, phase: str) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if await self._call(check):
                return True
            if time.monotonic() >= deadline:
                return False
            activity.heartbeat(phase)
            await asyncio.sleep(self._timeouts.interval_seconds)

    def _runtime(self, context: TeardownContext):
        runtime = self._runtimes[context.manifest.target.provider]
        if runtime is None:
            raise TeardownFailure("RUNTIME_NOT_CONFIGURED")
        if context.manifest.target.provider == "AWS":  # 46: the connection that created it
            try:
                credential_ref = AwsPlanInput.model_validate(context.spec).credential_ref
                profiles = resolve_aws_profiles(
                    credential_ref, platform_profile=getattr(runtime, "aws_profile", None)
                )
            except ProvisioningInputError as error:
                raise TeardownFailure(error.issues[0].code) from None
            except ValueError:
                raise TeardownFailure("ARTIFACT_DIGEST_MISMATCH") from None
            if profiles.connection_id is not None:
                runtime = runtime.with_profile(profiles.provider)
        return runtime

    async def _check_identity(self, runtime, context: TeardownContext) -> None:
        target = context.manifest.target
        try:
            if target.provider == "AWS":
                found = await self._call(runtime.account_id)
            else:
                found = await self._call(runtime.project_id)
        except Exception:  # noqa: BLE001 - CLI output is never surfaced
            raise TeardownFailure("IDENTITY_UNAVAILABLE") from None
        if found != target.account_id:
            raise TeardownFailure("ACCOUNT_MISMATCH")

    # ---- 1. workloads, load balancers, volumes ----

    async def _remove(self, cluster: TeardownCluster, context: TeardownContext, refs, delete_data):
        for namespace, app, db in context.releases:  # D3 ①: app before its database
            activity.heartbeat(f"helm:{namespace}")
            await self._call(cluster.helm_uninstall, namespace, app)
            await self._call(cluster.helm_uninstall, namespace, db)
        refs["releases_removed"] = str(2 * len(context.releases))

        objects = await self._call(cluster.load_balancer_objects)  # D3 ②
        for kind, namespace, name in objects:
            await self._call(cluster.delete_object, kind, namespace, name)
        refs["load_balancer_objects_deleted"] = str(len(objects))
        gone = await self._wait(
            lambda: not cluster.load_balancer_objects(),
            self._timeouts.load_balancer_seconds,
            "load-balancers",
        )
        if not gone:
            left = await self._call(cluster.load_balancer_objects)
            refs["load_balancer_objects_pending"] = str(len(left))
            raise TeardownFailure("LOAD_BALANCER_DELETE_PENDING", "UNKNOWN")

        namespaces = sorted({namespace for namespace, _, _ in context.releases})  # D3 ③ / D4
        if delete_data:
            deleting = []
            for namespace in namespaces:
                for claim in await self._call(cluster.claims, namespace):
                    if claim.get("volume"):
                        await self._call(cluster.set_reclaim_policy, claim["volume"], "Delete")
                        deleting.append(claim["volume"])
                    await self._call(cluster.delete_object, "pvc", namespace, claim["name"])
            refs["deleted_volume_count"] = str(len(deleting))

            def volumes_gone():
                names = {pv["name"] for pv in cluster.persistent_volumes()}
                return not names & set(deleting)

            if not await self._wait(volumes_gone, self._timeouts.volume_seconds, "volumes"):
                raise TeardownFailure("VOLUME_DELETE_PENDING", "UNKNOWN")
        retained = [
            pv
            for pv in await self._call(cluster.persistent_volumes)
            if pv.get("reclaim") == "Retain"
        ]
        refs["retained_volume_count"] = str(len(retained))
        if retained:  # disk IDs survive the cluster (data kept by policy)
            refs["retained_volume_ids"] = ",".join(
                sorted(str(pv.get("handle") or pv["name"]) for pv in retained)
            )

    async def _workloads(self, request: dict) -> dict:
        refs: dict[str, str] = {"delete_data": str(bool(request.get("delete_data"))).lower()}
        try:
            context = await self._call(self._context, request)
            if context.cluster is None:  # Apply never produced a cluster: nothing to uninstall
                refs["cluster"] = "NOT_CREATED"
                return outcome("SUCCEEDED", "NO_CLUSTER", refs)
            runtime = self._runtime(context)
            await self._check_identity(runtime, context)
            activity.heartbeat("connect")
            connection = runtime.connect(context.cluster)
            try:
                cluster = await self._call(connection.__enter__)
            except Exception:  # noqa: BLE001 - no kubeconfig or CLI output in results
                raise TeardownFailure("CLUSTER_ACCESS_FAILED", "UNKNOWN") from None
            try:
                await self._remove(cluster, context, refs, bool(request.get("delete_data")))
            finally:
                await self._call(connection.__exit__, None, None, None)
        except TeardownFailure as failure:
            return outcome(failure.status, failure.code, refs)
        except Exception:  # noqa: BLE001 - boundary: codes only; the Activity may be retried
            activity.logger.warning("teardown_cloud_workloads interrupted", extra={"refs": refs})
            return outcome("UNKNOWN", "TEARDOWN_INTERRUPTED", refs)
        return outcome("SUCCEEDED", "WORKLOADS_REMOVED", refs)

    @activity.defn(name="teardown_cloud_workloads")
    async def teardown_cloud_workloads(self, request: dict) -> dict:
        return await self._workloads(request)

    # ---- 2./3. reviewed Terraform destroy ----

    def _destroy(self, fn, request: dict, **extra) -> dict:
        try:
            context = self._context(request)
        except TeardownFailure as failure:
            return outcome(failure.status, failure.code, {})
        return fn(
            context.manifest,
            context.artifact_dir,
            aws_profile=self._aws_profile or "",
            gcloud_configuration=self._gcloud_configuration,
            expected_adc_email=self._adc_email,
            adc_email=self._adc_lookup,
            terraform_bin=self._terraform_bin,
            runner=self._runner,
            **extra,
        )

    @activity.defn(name="plan_cloud_destroy")
    async def plan_cloud_destroy(self, request: dict) -> dict:
        return await run_with_heartbeat(self._destroy, plan_destroy, request)

    @activity.defn(name="apply_cloud_destroy")
    async def apply_cloud_destroy(self, request: dict) -> dict:
        def run():
            return self._destroy(apply_destroy, request, review_digest=request["review_digest"])

        return await run_with_heartbeat(run)


def build_cloud_teardown_activities(session_factory) -> CloudTeardownActivities:
    """Worker wiring: the same tools, profiles and ADC principal as 08~11 (46 connection rules)."""
    import os

    from cmp_worker.activities.aws_workloads_cli import CliRuntime
    from cmp_worker.activities.gcp_workloads_cli import GcpCliRuntime
    from cmp_worker.config import settings

    tools = {
        "kubectl": os.environ.get("KUBECTL_BIN", "kubectl"),
        "helm": os.environ.get("HELM_BIN", "helm"),
    }
    return CloudTeardownActivities(
        session_factory,
        artifact_root=settings.provisioning_artifact_root,
        aws_runtime=CliRuntime(
            **tools,
            aws=os.environ.get("AWS_BIN", "aws"),
            aws_profile=settings.provisioning_aws_profile,
        ),
        gcp_runtime=GcpCliRuntime(
            **tools,
            gcloud=os.environ.get("GCLOUD_BIN", "gcloud"),
            aws=os.environ.get("AWS_BIN", "aws"),
            gcloud_configuration=settings.provisioning_gcloud_configuration,
            aws_profile=settings.provisioning_aws_profile,
        ),
        aws_profile=settings.provisioning_aws_profile,
        gcloud_configuration=settings.provisioning_gcloud_configuration,
        adc_email=settings.provisioning_gcp_adc_email,
        terraform_bin=settings.terraform_bin,
    )
