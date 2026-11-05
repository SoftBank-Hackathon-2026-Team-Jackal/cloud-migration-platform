"""Ticket 09 seam: real execute_aws_workloads Activity + SQLite business DB + real artifact bytes.

Only the cluster/Helm/AWS/HTTP boundary is a stateful fake. Expected outcomes come from the
ticket 09 decisions (W1~W11), not from the implementation.
"""

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
from cmp_worker.activities.aws_workloads import (
    AwsWorkloadsActivities,
    ClusterTarget,
    Timeouts,
)
from network_policy_support import NetworkPolicyFake
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

FIXTURES = Path(__file__).parent / "fixtures/contracts"
ACCOUNT = "714066696240"
NAMESPACE = "cmp-demo"
APP_IMAGE = "714066696240.dkr.ecr.ap-northeast-2.amazonaws.com/cmp-demo-api@sha256:" + "b" * 64
DB_IMAGE = "docker.io/library/postgres@sha256:" + "c" * 64
TARGET = ClusterTarget(
    cluster_name="cmp-target",
    region="ap-northeast-2",
    vpc_id="vpc-0123456789abcdef0",
    lb_controller_role_arn="arn:aws:iam::714066696240:role/cmp-target-lbc",
)
ALB = "k8s-cmpdemo-cmpdemoa-0000000000-1.ap-northeast-2.elb.amazonaws.com"


def chart_tgz(name: str, version: str = "0.1.0") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = f"apiVersion: v2\nname: {name}\nversion: {version}\n".encode()
        info = tarfile.TarInfo(f"{name}/Chart.yaml")
        info.size = len(content)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def db_values() -> dict:
    return {
        "contract_version": "1",
        "environment": "PRODUCTION",
        "provider": "AWS",
        "release_name": "cmp-demo-db",
        "namespace": NAMESPACE,
        "image": {"uri": DB_IMAGE, "platforms": ["linux/amd64"]},
        "database": {
            "version": "16",
            "name": "demoapp",
            "username": "demoapp",
            "secret_name": "cmp-demo-db-auth",
            "secret_key": "password",
        },
        "resources": {"cpu_millicores": 250, "memory_mib": 512},
        "storage": {"class_name": "cmp-gp3", "size_gib": 10},
    }


def app_template() -> dict:
    data = json.loads((FIXTURES / "app-values.json").read_text())
    data.update(
        environment="PRODUCTION", provider="AWS", release_name="cmp-demo-api", namespace=NAMESPACE
    )
    data["image"] = {"uri": APP_IMAGE, "platforms": ["linux/amd64"]}
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
    config = {"kind": "CONFIG_MAP", "name": "cmp-demo-config"}
    data["required_env"] = [
        "APP_PORT",
        "DEMO_WRITE_TOKEN",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
    ]
    data["env"] = [
        {"name": "APP_PORT", "source": {**config, "key": "APP_PORT"}},
        {
            "name": "DEMO_WRITE_TOKEN",
            "source": {
                "kind": "USER_SECRET",
                "name": "cmp-demo-secrets",
                "key": "DEMO_WRITE_TOKEN",
            },
        },
        {"name": "DB_HOST", "source": {"kind": "DATABASE", "field": "host"}},
        {"name": "DB_PORT", "source": {"kind": "DATABASE", "field": "port"}},
        {"name": "DB_NAME", "source": {"kind": "DATABASE", "field": "name"}},
        {"name": "DB_USER", "source": {"kind": "DATABASE", "field": "username"}},
        {"name": "DB_PASSWORD", "source": {"kind": "DATABASE", "field": "secret_ref"}},
    ]
    return prepare_application(data).values.model_dump(mode="json")


def bindings_bytes(template: dict) -> bytes:
    rules = prepare_application(template).bindings
    return canonical_json_bytes([rule.model_dump(mode="json") for rule in rules])


class Plan:
    """Writes the approved artifacts and publishes + approves one Revision."""

    def __init__(
        self, root: Path, db, *, app=None, image_uri=APP_IMAGE, scope="WORKLOADS", rules_from=None
    ):
        self.root = root
        app = app or app_template()
        # Binding rules are approved from a clean template; pass `rules_from` for custom apps.
        rules = bindings_bytes(rules_from or app_template())
        self.contents = {
            "spec": canonical_json_bytes({"spec": "demo"}),
            "database_chart": chart_tgz("postgres"),
            "database_values": canonical_json_bytes(db_values()),
            "application_chart": chart_tgz("application"),
            "application_values": canonical_json_bytes(app),
            "bindings": rules,
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
                "id": f"plan-009-r1-{role}",
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
                "migration_id": "migration-009",
                "plan": {"plan_id": "plan-009", "revision": 1, "template_version": "v1"},
                "target": {
                    "provider": "AWS",
                    "environment_id": "production-001",
                    "account_id": ACCOUNT,
                    "region": "ap-northeast-2",
                    "state_ref": {"id": "state-001", "version": "1"},
                },
                "scope": scope,
                "image_uri": image_uri,
                "artifacts": artifacts,
            }
        )
        folder = root / "plan-009-r1"
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
                session, approver_id="approver-1", migration_ids=["migration-009"]
            )
            approver = authenticate_approver(session, credential)
            record, _ = record_decision(
                session,
                approver=approver,
                plan_id="plan-009",
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
            "plan_id": "plan-009",
            "revision": 1,
            "provider": "AWS",
            "scope": self.manifest.scope,
            "manifest_digest": manifest_digest(self.manifest),
            **overrides,
        }

    def overwrite(self, role: str, content: bytes) -> None:
        digest = self.manifest.artifacts[role].sha256
        (self.root / "plan-009-r1" / digest).write_bytes(content)


class FakeCluster(NetworkPolicyFake):
    """Stateful stand-in for kubectl/Helm on one EKS cluster. Records every argument."""

    def __init__(self):
        self.namespaces: set[str] = set()
        self.storage_classes: dict[str, dict] = {}
        self.secrets: dict[tuple[str, str], dict[str, str]] = {
            (NAMESPACE, "cmp-demo-secrets"): {"DEMO_WRITE_TOKEN": "user-token-value"}
        }
        self.configmaps: dict[tuple[str, str], set[str]] = {
            (NAMESPACE, "cmp-demo-config"): {"APP_PORT"}
        }
        self.releases: dict[tuple[str, str], dict] = {}
        self.helm_calls: list[tuple[str, str, dict]] = []
        self.applied: list[dict] = []
        self.lb_controller_calls: list[ClusterTarget] = []
        self.pvc_phase = "Bound"
        self.waiting_reason: str | None = None
        self.db_probe = "160004|true|logical|10|10"
        self.internal_health = True
        self.alb_host: str | None = ALB
        self.trace: list[str] = []

    def _log(self, *args) -> None:
        self.trace.append(json.dumps(args, default=str))

    def ensure_lb_controller(self, target: ClusterTarget) -> None:
        self._log("lbc", target)
        self.lb_controller_calls.append(target)

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

    def secret_keys(self, namespace, name):
        data = self.secrets.get((namespace, name))
        return None if data is None else set(data)

    def create_secret(self, namespace, name, data):
        # Not logged to trace: the real runtime sends this only via stdin.
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

    def ingress_hostname(self, namespace, name):
        return self.alb_host


class FakeRuntime:
    def __init__(self, cluster: FakeCluster, account=ACCOUNT):
        self.cluster = cluster
        self.account = account
        self.connected = 0
        self.http_ok = True
        self.urls: list[str] = []

    def account_id(self) -> str:
        return self.account

    @contextmanager
    def connect(self, target: ClusterTarget):
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


FAST = Timeouts(readiness_seconds=0, alb_seconds=0, interval_seconds=0)


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
    return AwsWorkloadsActivities(
        db,
        artifact_root=root,
        resolver=resolver or FixedResolver(),
        runtime=runtime,
        timeouts=FAST,
    )


async def run(activities, request):
    return await ActivityEnvironment().run(activities.execute_aws_workloads, request)


async def test_ready_path_deploys_db_then_app_and_returns_only_refs(
    tmp_path, db, cluster, runtime, caplog
):
    caplog.set_level(logging.DEBUG)
    plan = Plan(tmp_path / "artifacts", db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())

    assert result["status"] == "SUCCEEDED"
    assert result["code"] == "WORKLOADS_READY"
    refs = result["resource_refs"]
    assert refs["cluster_name"] == "cmp-target"
    assert refs["namespace"] == NAMESPACE
    assert refs["db_host"] == "cmp-demo-db.cmp-demo.svc"
    assert refs["db_port"] == "5432"
    assert refs["db_secret_ref"] == "cmp-demo-db-auth/password"
    assert refs["app_external_endpoint"] == f"http://{ALB}"
    assert refs["app_image"] == APP_IMAGE
    assert all(isinstance(v, str) for v in refs.values())

    # DB Release before App Release; App Values carry the resolved DB slots only.
    assert [call[1] for call in cluster.helm_calls] == ["cmp-demo-db", "cmp-demo-api"]
    app_values = cluster.helm_calls[1][2]
    assert app_values["database"]["host"] == "cmp-demo-db.cmp-demo.svc"
    assert app_values["database"]["secret_name"] == "cmp-demo-db-auth"
    assert cluster.lb_controller_calls == [TARGET]

    storage = cluster.storage_classes["cmp-gp3"]
    assert storage["provisioner"] == "ebs.csi.aws.com"
    assert storage["parameters"]["type"] == "gp3"
    ingress = next(m for m in cluster.applied if m["kind"] == "Ingress")
    assert ingress["spec"]["ingressClassName"] == "alb"
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": "cmp-demo-api", "port": {"number": 8080}}
    assert runtime.urls == [f"http://{ALB}/health"]

    # Generated DB password exists in the cluster but nowhere in result, trace or logs.
    password = cluster.secrets[(NAMESPACE, "cmp-demo-db-auth")]["password"]
    assert len(password) >= 24
    exposed = json.dumps(result) + "\n".join(cluster.trace) + caplog.text
    assert password not in exposed
    assert "user-token-value" not in exposed


async def test_rerun_inspects_releases_and_does_not_reinstall_or_rotate(
    tmp_path, db, cluster, runtime
):
    plan = Plan(tmp_path / "artifacts", db)
    activities = activity_for(db, tmp_path / "artifacts", runtime)
    assert (await run(activities, plan.request()))["status"] == "SUCCEEDED"
    password = cluster.secrets[(NAMESPACE, "cmp-demo-db-auth")]["password"]

    again = await run(activities, plan.request())

    assert again["status"] == "SUCCEEDED"
    assert len(cluster.helm_calls) == 2  # no upgrade for identical Values/Chart
    assert cluster.secrets[(NAMESPACE, "cmp-demo-db-auth")]["password"] == password


async def test_changed_release_is_upgraded(tmp_path, db, cluster, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    cluster.releases[(NAMESPACE, "cmp-demo-db")] = {
        "status": "deployed",
        "chart": "postgres-0.1.0",
        "values": {"old": True},
    }
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert result["status"] == "SUCCEEDED"
    assert cluster.releases[(NAMESPACE, "cmp-demo-db")]["values"] == db_values()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("pending-install", ("UNKNOWN", "HELM_RELEASE_PENDING")),
        ("failed", ("FAILED", "HELM_RELEASE_FAILED")),
    ],
)
async def test_unfinished_release_is_not_touched(tmp_path, db, cluster, runtime, status, expected):
    plan = Plan(tmp_path / "artifacts", db)
    cluster.releases[(NAMESPACE, "cmp-demo-db")] = {
        "status": status,
        "chart": "postgres-0.1.0",
        "values": {},
    }
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == expected
    assert cluster.helm_calls == []


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"manifest_digest": "0" * 64}, "MANIFEST_DIGEST_MISMATCH"),
        ({"revision": 2}, "PLAN_NOT_FOUND"),
        ({"provider": "GCP"}, "PROVIDER_MISMATCH"),
        ({"scope": "INFRASTRUCTURE"}, "SCOPE_MISMATCH"),
        ({"approval_id": "missing"}, "APPROVAL_NOT_FOUND"),
    ],
)
async def test_request_mismatch_never_reaches_the_cluster(tmp_path, db, runtime, change, code):
    plan = Plan(tmp_path / "artifacts", db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request(**change))
    assert result == {"status": "FAILED", "code": code, "resource_refs": {}}
    assert runtime.connected == 0


async def test_stale_revision_never_reaches_the_cluster(tmp_path, db, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    newer = plan.manifest.model_dump(mode="json")
    newer["plan"]["revision"] = 2
    newer = PlanManifest.model_validate(newer)
    with db() as session:
        publish_plan_revision(
            session,
            newer,
            Preview(
                contract_version="1",
                manifest_digest=manifest_digest(newer),
                changes=[{"resource_type": "APP", "action": "CREATE", "name": "x"}],
            ),
        )
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "STALE_REVISION")
    assert runtime.connected == 0


@pytest.mark.parametrize("role", ["database_values", "application_chart", "bindings"])
async def test_tampered_artifact_bytes_block_execution(tmp_path, db, runtime, role):
    plan = Plan(tmp_path / "artifacts", db)
    plan.overwrite(role, plan.contents[role] + b" ")
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "ARTIFACT_DIGEST_MISMATCH")
    assert runtime.connected == 0


async def test_missing_artifact_blocks_execution(tmp_path, db, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    (tmp_path / "artifacts/plan-009-r1" / plan.manifest.artifacts["database_chart"].sha256).unlink()
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "ARTIFACT_MISSING")
    assert runtime.connected == 0


async def test_app_image_must_equal_the_approved_image(tmp_path, db, runtime):
    other = "714066696240.dkr.ecr.ap-northeast-2.amazonaws.com/cmp-demo-api@sha256:" + "d" * 64
    plan = Plan(tmp_path / "artifacts", db, image_uri=other)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "APP_IMAGE_MISMATCH")
    assert runtime.connected == 0


async def test_prefilled_database_slot_is_rejected(tmp_path, db, runtime):
    template = app_template()
    template["database"]["host"] = "attacker.example.com"
    plan = Plan(tmp_path / "artifacts", db, app=template)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "INVALID_APP_VALUES")
    assert runtime.connected == 0


async def test_account_mismatch_blocks_execution(tmp_path, db, cluster):
    plan = Plan(tmp_path / "artifacts", db)
    runtime = FakeRuntime(cluster, account="111111111111")
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "ACCOUNT_MISMATCH")
    assert runtime.connected == 0


async def test_unresolved_cluster_target_blocks_execution(tmp_path, db, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    activities = activity_for(db, tmp_path / "artifacts", runtime, FixedResolver(None))
    result = await run(activities, plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "CLUSTER_TARGET_UNAVAILABLE")
    assert runtime.connected == 0


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda c: c.secrets.pop((NAMESPACE, "cmp-demo-secrets")), "USER_SECRET_MISSING"),
        (
            lambda c: c.secrets.update({(NAMESPACE, "cmp-demo-secrets"): {"OTHER": "x"}}),
            "USER_SECRET_MISSING",
        ),
        (lambda c: c.configmaps.pop((NAMESPACE, "cmp-demo-config")), "CONFIG_MAP_MISSING"),
        (
            lambda c: c.secrets.update({(NAMESPACE, "cmp-demo-db-auth"): {"other": "x"}}),
            "DB_SECRET_INVALID",
        ),
        (
            lambda c: c.storage_classes.update(
                {"cmp-gp3": {"provisioner": "kubernetes.io/aws-ebs"}}
            ),
            "STORAGE_CLASS_MISMATCH",
        ),
    ],
)
async def test_reference_errors_fail_before_any_helm_release(
    tmp_path, db, cluster, runtime, setup, code
):
    plan = Plan(tmp_path / "artifacts", db)
    setup(cluster)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", code)
    assert cluster.helm_calls == []


def _pvc(c):
    c.pvc_phase = "Pending"


def _pull(c):
    c.waiting_reason = "ImagePullBackOff"


def _secret_ref(c):
    c.waiting_reason = "CreateContainerConfigError"


def _db(c):
    c.db_probe = None


def _db_version(c):
    c.db_probe = "150004|true"


def _health(c):
    c.internal_health = False


def _alb(c):
    c.alb_host = None


@pytest.mark.parametrize(
    ("break_it", "code"),
    [
        (_pvc, "PVC_NOT_BOUND"),
        (_pull, "IMAGE_PULL_FAILED"),
        (_secret_ref, "SECRET_REFERENCE_INVALID"),
        (_db, "DB_CONNECT_FAILED"),
        (_db_version, "DB_VERSION_MISMATCH"),
        (_health, "HEALTH_CHECK_FAILED"),
        (_alb, "ALB_ADDRESS_PENDING"),
    ],
)
async def test_any_failed_readiness_check_prevents_ready(
    tmp_path, db, cluster, runtime, break_it, code
):
    plan = Plan(tmp_path / "artifacts", db)
    break_it(cluster)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert result["status"] == "FAILED"
    assert result["code"] == code
    assert result["resource_refs"]["namespace"] == NAMESPACE  # partial refs stay visible


async def test_unreachable_alb_prevents_ready(tmp_path, db, cluster, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    runtime.http_ok = False
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "ALB_UNREACHABLE")
    assert result["resource_refs"]["alb_hostname"] == ALB
    assert "app_external_endpoint" not in result["resource_refs"]


async def test_lb_controller_failure_stops_before_workloads(tmp_path, db, cluster, runtime):
    plan = Plan(tmp_path / "artifacts", db)

    def broken(target):
        raise RuntimeError("helm failed")

    cluster.ensure_lb_controller = broken
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "LB_CONTROLLER_NOT_READY")
    assert cluster.helm_calls == []


async def test_cluster_access_failure_is_reported(tmp_path, db, cluster, runtime):
    plan = Plan(tmp_path / "artifacts", db)

    @contextmanager
    def refused(target):
        raise RuntimeError("aws eks update-kubeconfig failed")
        yield

    runtime.connect = refused
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "CLUSTER_ACCESS_FAILED")


def test_resolver_prefers_infrastructure_refs_passed_by_the_workflow(tmp_path, db):
    from cmp_worker.activities.aws_workloads import ExecutionRefsResolver

    plan = Plan(tmp_path / "artifacts", db)
    refs = {
        "eks_cluster_name": "cmp-target",
        "vpc_id": "vpc-0123456789abcdef0",
        "lb_controller_role_arn": "arn:aws:iam::714066696240:role/cmp-target-lbc",
        "node_group_name": "ignored",
    }
    with db() as session:
        found = ExecutionRefsResolver().resolve(
            session, plan.manifest, plan.request(resource_refs=refs)
        )
        missing = ExecutionRefsResolver().resolve(
            session, plan.manifest, plan.request(resource_refs={"vpc_id": "vpc-1"})
        )
    assert found == TARGET
    assert missing is None


@pytest.mark.skipif(
    not hasattr(__import__("cmp_api.approvals").approvals.PlanExecution, "resource_refs"),
    reason="ticket 08 execution refs schema is not merged yet",
)
def test_resolver_reads_latest_succeeded_infrastructure_execution(tmp_path, db):
    from cmp_api.approvals import PlanExecution
    from cmp_worker.activities.aws_workloads import ExecutionRefsResolver

    plan = Plan(tmp_path / "artifacts", db)
    infra = plan.manifest.model_dump(mode="json")
    infra["plan"]["plan_id"] = "plan-008"
    infra["scope"] = "INFRASTRUCTURE"
    infra = PlanManifest.model_validate(infra)
    refs = {
        "eks_cluster_name": "cmp-target",
        "vpc_id": "vpc-0123456789abcdef0",
        "lb_controller_role_arn": "arn:aws:iam::714066696240:role/cmp-target-lbc",
    }
    with db() as session:
        publish_plan_revision(
            session,
            infra,
            Preview(
                contract_version="1",
                manifest_digest=manifest_digest(infra),
                changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "c"}],
            ),
        )
        session.add(
            PlanExecution(
                plan_id="plan-008",
                revision=1,
                step="PROVISION",
                approval_id=plan.approval_id,
                status="SUCCEEDED",
                started_at=1,
                finished_at=2,
                resource_refs=refs,
            )
        )
        session.commit()
        found = ExecutionRefsResolver().resolve(session, plan.manifest, plan.request())
    assert found == TARGET


def test_cli_sends_secret_only_through_stdin_create(tmp_path):
    from cmp_worker.activities.aws_workloads_cli import CliRuntime, KubectlSession

    calls = []

    class Recording(CliRuntime):
        def run(self, args, *, stdin=None, timeout=300):
            calls.append((args, stdin))
            return b""

    session = KubectlSession(Recording(), tmp_path / "kubeconfig", tmp_path)
    session.create_secret(NAMESPACE, "cmp-demo-db-auth", {"password": "s3cret-value-xyz"})
    keys = session.secret_keys(NAMESPACE, "cmp-demo-db-auth")

    create_args, create_stdin = calls[0]
    assert "create" in create_args and "apply" not in create_args
    assert all("s3cret-value-xyz" not in arg for args, _ in calls for arg in args)
    assert b"s3cret-value-xyz" in create_stdin
    assert keys is None  # not found
    assert all("json" not in arg for arg in calls[1][0])  # key names only, never values


def test_cli_release_lookup_uses_flags_valid_on_helm_3_and_4(tmp_path):
    from cmp_worker.activities.aws_workloads_cli import CliRuntime, KubectlSession

    calls = []

    class Recording(CliRuntime):
        def run(self, args, *, stdin=None, timeout=300):
            calls.append(args)
            if "list" in args:
                return b'[{"name": "cmp-demo-db", "status": "pending-install", "chart": "postgres-0.1.0"}]'
            return b"{}"

    found = KubectlSession(Recording(), tmp_path / "k", tmp_path).release(NAMESPACE, "cmp-demo-db")

    listing = calls[0]
    assert "-a" not in listing  # removed in Helm 4
    assert {"--deployed", "--failed", "--pending"} <= set(listing)
    assert found == {"status": "pending-install", "chart": "postgres-0.1.0", "values": {}}


async def _through_workflow(db, plan, activities):
    import asyncio

    from cmp_api.approval_delivery import TemporalApprovalDelivery
    from cmp_contracts.queues import PROVISIONING_QUEUE, WORKFLOW_QUEUE, approval_workflow_id
    from cmp_worker.activities.approval import ApprovalActivities
    from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

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
                activities.execute_aws_workloads,
            ],
        ),
    ):
        await TemporalApprovalDelivery(env.client).deliver(
            approval_id=plan.approval_id, plan_id="plan-009", revision=1
        )
        handle = env.client.get_workflow_handle(approval_workflow_id("plan-009", 1))
        return await asyncio.wait_for(handle.result(), timeout=30)


def _marker(db, plan):
    from cmp_api.approvals import PlanApproval, execution_refs, execution_status

    with db() as session:
        record = session.get(PlanApproval, plan.approval_id)
        return execution_status(session, record), execution_refs(session, record)


async def test_approval_workflow_dispatches_aws_workloads_and_records_refs(
    tmp_path, db, cluster, runtime
):
    plan = Plan(tmp_path / "artifacts", db)
    outcome = await _through_workflow(db, plan, activity_for(db, tmp_path / "artifacts", runtime))

    assert (outcome["status"], outcome["code"]) == ("EXECUTED", "WORKLOADS_READY")
    status, refs = _marker(db, plan)
    assert status == "SUCCEEDED"
    assert refs["app_external_endpoint"] == f"http://{ALB}"
    password = cluster.secrets[(NAMESPACE, "cmp-demo-db-auth")]["password"]
    assert password not in json.dumps(refs)


async def test_approval_workflow_records_failed_readiness_without_ready(
    tmp_path, db, cluster, runtime
):
    plan = Plan(tmp_path / "artifacts", db)
    cluster.pvc_phase = "Pending"
    outcome = await _through_workflow(db, plan, activity_for(db, tmp_path / "artifacts", runtime))

    assert (outcome["status"], outcome["code"]) == ("FAILED", "PVC_NOT_BOUND")
    status, refs = _marker(db, plan)
    assert status == "FAILED"
    assert "app_external_endpoint" not in refs


@pytest.mark.parametrize(
    ("probe", "capable", "code"),
    [
        ("160004|true|logical|10|10", "true", None),
        ("160004|true|replica|10|10", "false", "WAL_LEVEL_NOT_LOGICAL"),
        ("160004|false|logical|10|10", "false", "REPLICATION_ROLE_MISSING"),
    ],
)
async def test_replication_capable_checks_role_and_server_settings(
    tmp_path, db, cluster, runtime, probe, capable, code
):
    """Ticket 27: a superuser on `wal_level=replica` is READY but not replication capable."""
    cluster.db_probe = probe
    plan = Plan(tmp_path / "artifacts", db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("SUCCEEDED", "WORKLOADS_READY")
    refs = result["resource_refs"]
    assert refs["db_replication_capable"] == capable
    assert refs.get("db_replication_code") == code
