"""Ticket 11 seam: real execute_gcp_workloads Activity + SQLite business DB + real artifact bytes.

Only the cluster/Helm/gcloud/AWS(ECR)/HTTP boundary is a stateful fake. Expected outcomes come
from the ticket 11 decisions (K1~K12), not from the implementation.
"""

import base64
import io
import json
import logging
import tarfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest
from cmp_api.approvals import (
    authenticate_approver,
    publish_plan_revision,
    record_decision,
    register_approver,
)
from cmp_api.db import Base
from cmp_contracts.plan import PlanManifest, Preview
from cmp_providers.application import prepare_application
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes, sha256_json
from cmp_providers.plans import manifest_digest
from cmp_worker.activities.gcp_workloads import (
    GcpWorkloadsActivities,
    GkeTarget,
    Timeouts,
)
from network_policy_support import NetworkPolicyFake
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

FIXTURES = Path(__file__).parent / "fixtures/contracts"
PROJECT = "sbh26-prod"
REGION = "asia-northeast3"
AWS_ACCOUNT = "714066696240"
NAMESPACE = "cmp-demo-target"
REGISTRY = "714066696240.dkr.ecr.ap-northeast-2.amazonaws.com"
APP_IMAGE = f"{REGISTRY}/cmp-demo-api@sha256:" + "b" * 64
DB_IMAGE = "docker.io/library/postgres@sha256:" + "c" * 64
TARGET = GkeTarget(project_id=PROJECT, cluster_name="cmp-target-gcp", location=REGION)
INGRESS_IP = "34.120.0.10"
ECR_TOKEN = "ecr-token-value-should-never-leak"


def chart_tgz(name: str, version: str = "0.1.0") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = f"apiVersion: v2\nname: {name}\nversion: {version}\n".encode()
        info = tarfile.TarInfo(f"{name}/Chart.yaml")
        info.size = len(content)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def db_values(provider="GCP", image=DB_IMAGE) -> dict:
    return {
        "contract_version": "1",
        "environment": "PRODUCTION",
        "provider": provider,
        "release_name": "cmp-demo-db",
        "namespace": NAMESPACE,
        "image": {"uri": image, "platforms": ["linux/amd64"]},
        "database": {
            "version": "16",
            "name": "demoapp",
            "username": "demoapp",
            "secret_name": "cmp-demo-db-auth",
            "secret_key": "password",
        },
        "resources": {"cpu_millicores": 250, "memory_mib": 512},
        "storage": {"class_name": "cmp-pd-balanced", "size_gib": 10},
    }


def app_template(image=APP_IMAGE) -> dict:
    data = json.loads((FIXTURES / "app-values.json").read_text())
    data.update(
        environment="PRODUCTION", provider="GCP", release_name="cmp-demo-api", namespace=NAMESPACE
    )
    data["image"] = {"uri": image, "platforms": ["linux/amd64"]}
    data["application"] = {
        "cpu_millicores": 250,
        "memory_mib": 256,
        "replicas": 1,
        "port": 8080,
        "health_path": "/health",
    }
    data["database"] = {
        "host": None,
        "port": 5432,
        "name": "demoapp",
        "username": "demoapp",
        "secret_name": None,
        "secret_key": "password",
    }
    data["required_env"] = ["APP_PORT", "DEMO_WRITE_TOKEN", "DB_HOST", "DB_PASSWORD"]
    data["env"] = [
        {
            "name": "APP_PORT",
            "source": {"kind": "CONFIG_MAP", "name": "cmp-demo-config", "key": "APP_PORT"},
        },
        {
            "name": "DEMO_WRITE_TOKEN",
            "source": {
                "kind": "USER_SECRET",
                "name": "cmp-demo-secrets",
                "key": "DEMO_WRITE_TOKEN",
            },
        },
        {"name": "DB_HOST", "source": {"kind": "DATABASE", "field": "host"}},
        {"name": "DB_PASSWORD", "source": {"kind": "DATABASE", "field": "secret_ref"}},
    ]
    return prepare_application(data).values.model_dump(mode="json")


class Plan:
    """Writes the approved artifacts and publishes + approves one GCP Revision."""

    def __init__(self, root: Path, db, *, app=None, database=None, scope="WORKLOADS"):
        self.root = root
        app = app or app_template()
        rules = prepare_application(app).bindings
        self.contents = {
            "spec": canonical_json_bytes({"spec": "demo"}),
            "database_chart": chart_tgz("postgres"),
            "database_values": canonical_json_bytes(database or db_values()),
            "application_chart": chart_tgz("application"),
            "application_values": canonical_json_bytes(app),
            "bindings": canonical_json_bytes([r.model_dump(mode="json") for r in rules]),
        }
        kinds = {
            "spec": "SPEC",
            "database_chart": "CHART",
            "database_values": "VALUES",
            "application_chart": "CHART",
            "application_values": "VALUES",
            "bindings": "BINDINGS",
        }
        artifacts = {
            role: {
                "id": f"plan-011-r1-{role}",
                "version": "1",
                "kind": kinds[role],
                "sha256": sha256_bytes(content),
                "size_bytes": len(content),
            }
            for role, content in self.contents.items()
        }
        self.manifest = PlanManifest.model_validate(
            {
                "contract_version": "1",
                "digest_format": "cmp-json-v1",
                "migration_id": "migration-011",
                "plan": {"plan_id": "plan-011", "revision": 1, "template_version": "v1"},
                "target": {
                    "provider": "GCP",
                    "environment_id": "production-gcp",
                    "account_id": PROJECT,
                    "region": REGION,
                    "state_ref": {"id": "state-gcp", "version": "1"},
                },
                "scope": scope,
                "image_uri": app["image"]["uri"],
                "artifacts": artifacts,
            }
        )
        folder = root / "plan-011-r1"
        folder.mkdir(parents=True, exist_ok=True)
        for content in self.contents.values():
            (folder / sha256_bytes(content)).write_bytes(content)
        preview = Preview(
            contract_version="1",
            manifest_digest=manifest_digest(self.manifest),
            changes=[
                {"resource_type": "DATABASE", "action": "CREATE", "name": "cmp-demo-db"},
                {"resource_type": "APP", "action": "CREATE", "name": "cmp-demo-api"},
                {"resource_type": "INGRESS", "action": "CREATE", "name": "cmp-demo-api"},
            ],
        )
        with db() as session:
            publish_plan_revision(session, self.manifest, preview)
            credential = register_approver(
                session, approver_id="approver-1", migration_ids=["migration-011"]
            )
            approver = authenticate_approver(session, credential)
            record, _ = record_decision(
                session,
                approver=approver,
                plan_id="plan-011",
                revision=1,
                decision="APPROVE",
                scope=scope,
                manifest_digest=manifest_digest(self.manifest),
                preview_digest=sha256_json(preview.model_dump(mode="json")),
            )
            self.approval_id = record.id

    def request(self, **overrides) -> dict:
        return {
            "approval_id": self.approval_id,
            "plan_id": "plan-011",
            "revision": 1,
            "provider": "GCP",
            "scope": self.manifest.scope,
            "manifest_digest": manifest_digest(self.manifest),
            **overrides,
        }


class FakeCluster(NetworkPolicyFake):
    """Stateful stand-in for kubectl/Helm on one GKE cluster. Records every argument."""

    def __init__(self):
        self.namespaces: set[str] = set()
        self.storage_classes: dict[str, dict] = {}
        self.secrets: dict[tuple[str, str], dict[str, str]] = {
            (NAMESPACE, "cmp-demo-secrets"): {"DEMO_WRITE_TOKEN": "user-token-value"}
        }
        self.secret_types: dict[tuple[str, str], str] = {}
        self.configmaps = {(NAMESPACE, "cmp-demo-config"): {"APP_PORT"}}
        self.releases: dict[tuple[str, str], dict] = {}
        self.helm_calls: list[tuple[str, str, dict]] = []
        self.applied: list[dict] = []
        self.pvc_phase = "Bound"
        self.waiting_reason: str | None = None
        self.db_probe = "160004|true"
        self.internal_health = True
        self.ingress_ip: str | None = INGRESS_IP
        self.trace: list[str] = []

    def _log(self, *args) -> None:
        self.trace.append(json.dumps(args, default=str))

    def storage_class(self, name):
        return self.storage_classes.get(name)

    def ensure_namespace(self, name):
        self._log("namespace", name)
        self.namespaces.add(name)

    def apply(self, manifest: dict) -> None:
        self._log("apply", manifest)
        self.applied.append(manifest)
        if manifest["kind"] == "StorageClass":
            self.storage_classes[manifest["metadata"]["name"]] = manifest

    def apply_secret(self, namespace, name, *args):
        # Not logged to trace: the real runtime sends this only via stdin.
        secret_type, data = args if len(args) == 2 else ("Opaque", args[0])
        self.secrets[(namespace, name)] = dict(data)
        self.secret_types[(namespace, name)] = secret_type

    def secret_keys(self, namespace, name):
        data = self.secrets.get((namespace, name))
        return None if data is None else set(data)

    def create_secret(self, namespace, name, data):
        self.secrets[(namespace, name)] = dict(data)

    def configmap_keys(self, namespace, name):
        return self.configmaps.get((namespace, name))

    def release(self, namespace, name):
        return deepcopy(self.releases.get((namespace, name)))

    def helm_upgrade_install(self, namespace, name, chart: Path, values: dict):
        self._log("helm", namespace, name, values)
        with tarfile.open(chart) as archive:
            meta = archive.extractfile(archive.getmembers()[0]).read().decode()
        chart_name = meta.split("name: ")[1].split("\n")[0]
        version = meta.split("version: ")[1].split("\n")[0]
        self.helm_calls.append((namespace, name, values))
        self.releases[(namespace, name)] = {
            "status": "deployed",
            "chart": f"{chart_name}-{version}",
            "values": deepcopy(values),
        }

    def pvc_phase_of(self, namespace, name):
        return self.pvc_phase

    def workload_ready(self, namespace, kind, name):
        return self.waiting_reason is None

    def waiting_reasons(self, namespace, instance):
        return {self.waiting_reason} if self.waiting_reason else set()

    def database_probe(self, namespace, pod, host):
        self._log("db_probe", namespace, pod, host)
        if self.db_probe is None:
            raise RuntimeError("psql failed")
        return self.db_probe

    def service_health(self, namespace, service, path):
        return self.internal_health

    def ingress_address(self, namespace, name):
        return self.ingress_ip

    def applied_kind(self, kind):
        return [m for m in self.applied if m["kind"] == kind]


class FakeRuntime:
    def __init__(self, cluster: FakeCluster):
        self.cluster = cluster
        self.project = PROJECT
        self.status = "RUNNING"
        self.aws_account = AWS_ACCOUNT
        self.token_regions: list[str] = []
        self.token_error = False
        self.connected = 0
        self.http_ok = True
        self.urls: list[str] = []

    def project_id(self) -> str:
        return self.project

    def cluster_status(self, target: GkeTarget) -> str:
        return self.status

    def aws_account_id(self) -> str:
        return self.aws_account

    def ecr_password(self, region: str) -> str:
        self.token_regions.append(region)
        if self.token_error:
            raise RuntimeError("aws ecr failed")
        return ECR_TOKEN

    @contextmanager
    def connect(self, target: GkeTarget):
        self.connected += 1
        yield self.cluster

    def http_get_ok(self, url: str) -> bool:
        self.urls.append(url)
        return self.http_ok


class FixedResolver:
    def __init__(self, target=TARGET):
        self.target = target

    def resolve(self, session, manifest, request):
        return self.target


FAST = Timeouts(readiness_seconds=0, ingress_seconds=0, interval_seconds=0)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def runtime(cluster):
    return FakeRuntime(cluster)


def activity_for(db, root, runtime, resolver=None):
    return GcpWorkloadsActivities(
        db,
        artifact_root=root,
        resolver=resolver or FixedResolver(),
        runtime=runtime,
        timeouts=FAST,
    )


async def run(activities, request):
    return await ActivityEnvironment().run(activities.execute_gcp_workloads, request)


async def test_ready_path_deploys_on_gke_and_returns_only_refs(
    tmp_path, db, cluster, runtime, caplog
):
    caplog.set_level(logging.DEBUG)
    plan = Plan(tmp_path / "artifacts", db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())

    assert (result["status"], result["code"]) == ("SUCCEEDED", "WORKLOADS_READY")
    refs = result["resource_refs"]
    assert refs["gcp_project_id"] == PROJECT
    assert refs["gke_cluster_name"] == "cmp-target-gcp"
    assert refs["gke_location"] == REGION
    assert refs["namespace"] == NAMESPACE
    assert refs["db_host"] == "cmp-demo-db.cmp-demo-target.svc"
    assert refs["db_secret_ref"] == "cmp-demo-db-auth/password"
    assert refs["ingress_ip"] == INGRESS_IP
    assert refs["app_external_endpoint"] == f"http://{INGRESS_IP}"
    assert refs["image_pull_secret"] == "cmp-ecr-pull"
    # Ticket 13 R4: approved, non-secret values for the Migration handoff.
    assert (refs["db_version"], refs["db_storage_size_gib"]) == ("16", "10")
    assert (refs["app_port"], refs["app_health_path"], refs["app_replicas"]) == (
        "8080",
        "/health",
        "1",
    )
    assert all(isinstance(v, str) for v in refs.values())
    assert runtime.urls == [f"http://{INGRESS_IP}/health"]

    assert [call[1] for call in cluster.helm_calls] == ["cmp-demo-db", "cmp-demo-api"]
    app_values = cluster.helm_calls[1][2]
    assert app_values["database"]["host"] == "cmp-demo-db.cmp-demo-target.svc"

    # K7: Persistent Disk CSI, pd-balanced, Retain.
    storage = cluster.storage_classes["cmp-pd-balanced"]
    assert storage["provisioner"] == "pd.csi.storage.gke.io"
    assert storage["parameters"]["type"] == "pd-balanced"
    assert storage["reclaimPolicy"] == "Retain"
    assert storage["volumeBindingMode"] == "WaitForFirstConsumer"

    # K6: GCE Ingress, no AWS ALB pieces; NEG annotation on the chart Service.
    (ingress,) = cluster.applied_kind("Ingress")
    assert ingress["metadata"]["annotations"]["kubernetes.io/ingress.class"] == "gce"
    assert "ingressClassName" not in ingress["spec"]
    assert not any("alb" in key for key in ingress["metadata"]["annotations"])
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": "cmp-demo-api", "port": {"number": 8080}}
    (service,) = cluster.applied_kind("Service")
    assert service["metadata"]["name"] == "cmp-demo-api"
    assert json.loads(service["metadata"]["annotations"]["cloud.google.com/neg"]) == {
        "ingress": True
    }
    assert "spec" not in service  # annotation only; the chart owns the Service spec

    # K4: ECR pull Secret + default ServiceAccount link, token never exposed.
    assert runtime.token_regions == ["ap-northeast-2"]
    assert cluster.secret_types[(NAMESPACE, "cmp-ecr-pull")] == "kubernetes.io/dockerconfigjson"
    config = json.loads(cluster.secrets[(NAMESPACE, "cmp-ecr-pull")][".dockerconfigjson"])
    auth = config["auths"][REGISTRY]
    assert base64.b64decode(auth["auth"]).decode() == f"AWS:{ECR_TOKEN}"
    (account,) = cluster.applied_kind("ServiceAccount")
    assert account["metadata"] == {"name": "default", "namespace": NAMESPACE}
    assert account["imagePullSecrets"] == [{"name": "cmp-ecr-pull"}]
    pull_index = cluster.trace.index(next(t for t in cluster.trace if "ServiceAccount" in t))
    helm_index = cluster.trace.index(next(t for t in cluster.trace if '"helm"' in t))
    assert pull_index < helm_index

    password = cluster.secrets[(NAMESPACE, "cmp-demo-db-auth")]["password"]
    exposed = json.dumps(result) + "\n".join(cluster.trace) + caplog.text
    assert password not in exposed
    assert ECR_TOKEN not in exposed
    assert "user-token-value" not in exposed


async def test_rerun_skips_releases_refreshes_pull_token_and_keeps_db_password(
    tmp_path, db, cluster, runtime
):
    plan = Plan(tmp_path / "artifacts", db)
    activities = activity_for(db, tmp_path / "artifacts", runtime)
    assert (await run(activities, plan.request()))["status"] == "SUCCEEDED"
    password = cluster.secrets[(NAMESPACE, "cmp-demo-db-auth")]["password"]

    again = await run(activities, plan.request())

    assert again["code"] == "WORKLOADS_READY"
    assert len(cluster.helm_calls) == 2
    assert cluster.secrets[(NAMESPACE, "cmp-demo-db-auth")]["password"] == password
    assert runtime.token_regions == ["ap-northeast-2", "ap-northeast-2"]


async def test_public_images_need_no_pull_secret(tmp_path, db, cluster, runtime):
    public = "docker.io/library/nginx@sha256:" + "e" * 64
    plan = Plan(tmp_path / "artifacts", db, app=app_template(image=public))
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert result["code"] == "WORKLOADS_READY"
    assert runtime.token_regions == []
    assert (NAMESPACE, "cmp-ecr-pull") not in cluster.secrets
    assert cluster.applied_kind("ServiceAccount") == []
    assert "image_pull_secret" not in result["resource_refs"]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"provider": "AWS"}, "PROVIDER_MISMATCH"),
        ({"manifest_digest": "0" * 64}, "MANIFEST_DIGEST_MISMATCH"),
        ({"scope": "INFRASTRUCTURE"}, "SCOPE_MISMATCH"),
    ],
)
async def test_request_mismatch_never_reaches_the_cluster(tmp_path, db, runtime, change, code):
    plan = Plan(tmp_path / "artifacts", db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request(**change))
    assert result == {"status": "FAILED", "code": code, "resource_refs": {}}
    assert runtime.connected == 0


async def test_aws_values_are_rejected_for_gcp(tmp_path, db, runtime):
    plan = Plan(tmp_path / "artifacts", db, database=db_values(provider="AWS"))
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "INVALID_VALUES")
    assert runtime.connected == 0


async def test_tampered_artifact_blocks_execution(tmp_path, db, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    digest = plan.manifest.artifacts["application_values"].sha256
    (tmp_path / "artifacts/plan-011-r1" / digest).write_bytes(b"{}")
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "ARTIFACT_DIGEST_MISMATCH")
    assert runtime.connected == 0


@pytest.mark.parametrize(
    ("target", "code"),
    [
        (GkeTarget("other-project", "cmp-target-gcp", REGION), "PROJECT_MISMATCH"),
        (GkeTarget(PROJECT, "cmp-target-gcp", "us-central1"), "REGION_MISMATCH"),
        (GkeTarget(PROJECT, "cmp-target-gcp", "asia-northeast"), "REGION_MISMATCH"),
    ],
)
async def test_resolved_cluster_must_be_the_approved_target(tmp_path, db, runtime, target, code):
    plan = Plan(tmp_path / "artifacts", db)
    activities = activity_for(db, tmp_path / "artifacts", runtime, FixedResolver(target))
    result = await run(activities, plan.request())
    assert (result["status"], result["code"]) == ("FAILED", code)
    assert runtime.connected == 0


async def test_zonal_location_inside_the_region_is_accepted(tmp_path, db, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    zonal = GkeTarget(PROJECT, "cmp-target-gcp", f"{REGION}-a")
    activities = activity_for(db, tmp_path / "artifacts", runtime, FixedResolver(zonal))
    assert (await run(activities, plan.request()))["code"] == "WORKLOADS_READY"


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda r: setattr(r, "project", "other-project"), "PROJECT_MISMATCH"),
        (lambda r: setattr(r, "status", "PROVISIONING"), "CLUSTER_NOT_RUNNING"),
        (lambda r: setattr(r, "aws_account", "111111111111"), "ECR_ACCOUNT_MISMATCH"),
    ],
)
async def test_identity_checks_run_before_cluster_access(tmp_path, db, runtime, setup, code):
    plan = Plan(tmp_path / "artifacts", db)
    setup(runtime)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", code)
    assert runtime.connected == 0


async def test_unresolved_cluster_blocks_execution(tmp_path, db, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    activities = activity_for(db, tmp_path / "artifacts", runtime, FixedResolver(None))
    result = await run(activities, plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "CLUSTER_TARGET_UNAVAILABLE")


async def test_pull_token_failure_stops_before_helm(tmp_path, db, cluster, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    runtime.token_error = True
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "IMAGE_PULL_SECRET_FAILED")
    assert cluster.helm_calls == []


async def test_existing_non_pd_storage_class_is_rejected(tmp_path, db, cluster, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    cluster.storage_classes["cmp-pd-balanced"] = {"provisioner": "kubernetes.io/gce-pd"}
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "STORAGE_CLASS_MISMATCH")
    assert cluster.helm_calls == []


def _pvc(c):
    c.pvc_phase = "Pending"


def _pull(c):
    c.waiting_reason = "ErrImagePull"


def _db(c):
    c.db_probe = None


def _health(c):
    c.internal_health = False


def _address(c):
    c.ingress_ip = None


@pytest.mark.parametrize(
    ("break_it", "code"),
    [
        (_pvc, "PVC_NOT_BOUND"),
        (_pull, "IMAGE_PULL_FAILED"),
        (_db, "DB_CONNECT_FAILED"),
        (_health, "HEALTH_CHECK_FAILED"),
        (_address, "INGRESS_ADDRESS_PENDING"),
    ],
)
async def test_any_failed_readiness_check_prevents_ready(
    tmp_path, db, cluster, runtime, break_it, code
):
    plan = Plan(tmp_path / "artifacts", db)
    break_it(cluster)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", code)
    assert "app_external_endpoint" not in result["resource_refs"]


async def test_unreachable_ingress_prevents_ready(tmp_path, db, cluster, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    runtime.http_ok = False
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "INGRESS_UNREACHABLE")
    assert result["resource_refs"]["ingress_ip"] == INGRESS_IP
    assert "app_external_endpoint" not in result["resource_refs"]


REFS = {
    "gcp_project_id": PROJECT,
    "gke_cluster_name": "cmp-target-gcp",
    "gke_location": REGION,
    "vpc_network": "cmp-target-gcp",
    "gke_cluster_status": "RUNNING",
}


def test_resolver_prefers_refs_passed_by_the_workflow(tmp_path, db):
    from cmp_worker.activities.gcp_workloads import ExecutionRefsResolver

    plan = Plan(tmp_path / "artifacts", db)
    with db() as session:
        found = ExecutionRefsResolver().resolve(
            session, plan.manifest, plan.request(resource_refs=REFS)
        )
        missing = ExecutionRefsResolver().resolve(
            session, plan.manifest, plan.request(resource_refs={"gke_cluster_name": "x"})
        )
    assert found == TARGET
    assert missing is None


def test_resolver_reads_latest_succeeded_gcp_infrastructure_execution(tmp_path, db):
    from cmp_api.approvals import PlanExecution
    from cmp_worker.activities.gcp_workloads import ExecutionRefsResolver

    plan = Plan(tmp_path / "artifacts", db)
    infra = plan.manifest.model_dump(mode="json")
    infra["plan"]["plan_id"] = "plan-010"
    infra["scope"] = "INFRASTRUCTURE"
    infra = PlanManifest.model_validate(infra)
    other = infra.model_dump(mode="json")
    other["plan"]["plan_id"] = "plan-010b"
    other["target"]["environment_id"] = "another-env"
    other = PlanManifest.model_validate(other)
    with db() as session:
        for manifest in (infra, other):
            publish_plan_revision(
                session,
                manifest,
                Preview(
                    contract_version="1",
                    manifest_digest=manifest_digest(manifest),
                    changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "c"}],
                ),
            )
        for plan_id, name, finished in (
            ("plan-010", "cmp-target-gcp", 2),
            ("plan-010b", "wrong-env-cluster", 3),
        ):
            session.add(
                PlanExecution(
                    plan_id=plan_id,
                    revision=1,
                    step="PROVISION",
                    approval_id=plan.approval_id,
                    status="SUCCEEDED",
                    started_at=1,
                    finished_at=finished,
                    resource_refs={**REFS, "gke_cluster_name": name},
                )
            )
        session.commit()
        found = ExecutionRefsResolver().resolve(session, plan.manifest, plan.request())
    assert found == TARGET


# ---- CLI adapter: arguments, stdin and environment only (no real tools) ----


class Recorder:
    def __init__(self, outputs=None):
        self.calls: list[tuple[list[str], bytes | None, dict]] = []
        self.outputs = outputs or {}

    def __call__(self, runtime, args, stdin, timeout, env):
        self.calls.append((list(args), stdin, {**runtime._env, **(env or {})}))
        for marker, out in self.outputs.items():
            if marker in args:
                return out
        return b""


def recording_runtime(recorder, **kwargs):
    from cmp_worker.activities.gcp_workloads_cli import GcpCliRuntime

    class Recording(GcpCliRuntime):
        def run(self, args, *, stdin=None, timeout=300, env=None):
            return recorder(self, args, stdin, timeout, env)

    return Recording(gcloud_configuration="cmp-gcp-prod", aws_profile="cmp-aws-prod", **kwargs)


def test_cli_pull_secret_goes_through_stdin_server_side_apply(tmp_path):
    from cmp_worker.activities.gcp_workloads_cli import GkeKubectlSession

    recorder = Recorder()
    session = GkeKubectlSession(recording_runtime(recorder), tmp_path / "k", tmp_path)
    data = {".dockerconfigjson": json.dumps({"auths": {REGISTRY: {"password": ECR_TOKEN}}})}
    session.apply_secret(NAMESPACE, "cmp-ecr-pull", "kubernetes.io/dockerconfigjson", data)

    ((args, stdin, _),) = recorder.calls
    assert "apply" in args and "--server-side" in args and args[-2:] == ["-f", "-"]
    assert all(ECR_TOKEN not in arg for arg in args)
    body = json.loads(stdin)
    assert body["type"] == "kubernetes.io/dockerconfigjson"
    assert "stringData" in body and ECR_TOKEN in body["stringData"][".dockerconfigjson"]


def test_cli_ecr_token_and_identity_use_named_profiles(tmp_path):
    recorder = Recorder(
        {"get-login-password": b"tok\n", "get-value": b"sbh26-prod\n", "describe": b"RUNNING\n"}
    )
    runtime = recording_runtime(recorder)

    assert runtime.ecr_password("ap-northeast-2") == "tok"
    assert runtime.project_id() == PROJECT
    assert runtime.cluster_status(TARGET) == "RUNNING"

    ecr, project, describe = (call for call in recorder.calls)
    assert ecr[0][1:] == ["ecr", "get-login-password", "--region", "ap-northeast-2"]
    assert ecr[2]["AWS_PROFILE"] == "cmp-aws-prod"
    assert "--configuration" in project[0] and "cmp-gcp-prod" in project[0]
    assert describe[0][1:4] == ["container", "clusters", "describe"]
    assert {"--project", PROJECT, "--location", REGION} <= set(describe[0])
    assert describe[2]["CLOUDSDK_ACTIVE_CONFIG_NAME"] == "cmp-gcp-prod"


def test_cli_connect_writes_scratch_kubeconfig_only(tmp_path):
    recorder = Recorder()
    runtime = recording_runtime(recorder)
    with runtime.connect(TARGET) as session:
        ((args, _, env),) = recorder.calls
        kubeconfig = Path(env["KUBECONFIG"])
        assert kubeconfig.parent.stat().st_mode & 0o777 == 0o700
        assert session._kubeconfig == str(kubeconfig)
    assert args[1:5] == ["container", "clusters", "get-credentials", "cmp-target-gcp"]
    assert {"--project", PROJECT, "--location", REGION} <= set(args)
    assert str(Path.home() / ".kube") not in str(kubeconfig)
    assert not kubeconfig.parent.exists()  # deleted after the run


def test_cli_ingress_address_reads_gke_ip(tmp_path):
    from cmp_worker.activities.gcp_workloads_cli import GkeKubectlSession

    status = {"status": {"loadBalancer": {"ingress": [{"ip": INGRESS_IP}]}}}
    recorder = Recorder({"ingress": json.dumps(status).encode()})
    session = GkeKubectlSession(recording_runtime(recorder), tmp_path / "k", tmp_path)
    assert session.ingress_address(NAMESPACE, "cmp-demo-api") == INGRESS_IP


async def test_approval_workflow_dispatches_gcp_workloads(tmp_path, db, cluster, runtime):
    import asyncio

    from cmp_api.approval_delivery import TemporalApprovalDelivery
    from cmp_api.approvals import PlanApproval, execution_refs, execution_status
    from cmp_contracts.queues import PROVISIONING_QUEUE, WORKFLOW_QUEUE, approval_workflow_id
    from cmp_worker.activities.approval import ApprovalActivities
    from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    plan = Plan(tmp_path / "artifacts", db)
    activities = activity_for(db, tmp_path / "artifacts", runtime)
    approval = ApprovalActivities(db)
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
        Worker(
            env.client,
            task_queue=PROVISIONING_QUEUE,
            activities=[
                approval.claim_approved_execution,
                approval.complete_approved_execution,
                activities.execute_gcp_workloads,
            ],
        ),
    ):
        await TemporalApprovalDelivery(env.client).deliver(
            approval_id=plan.approval_id, plan_id="plan-011", revision=1
        )
        handle = env.client.get_workflow_handle(approval_workflow_id("plan-011", 1))
        result = await asyncio.wait_for(handle.result(), timeout=30)

    assert (result["status"], result["code"]) == ("EXECUTED", "WORKLOADS_READY")
    with db() as session:
        record = session.get(PlanApproval, plan.approval_id)
        assert execution_status(session, record) == "SUCCEEDED"
        refs = execution_refs(session, record)
    assert refs["app_external_endpoint"] == f"http://{INGRESS_IP}"
    assert ECR_TOKEN not in json.dumps(refs)
