import base64
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cmp_contracts.provisioning import Image
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_contracts.shared_deployment_v2 import SharedDeploymentRequestV2
from cmp_worker.sandbox_deploy import (
    SANDBOX_CONTEXT,
    SandboxCapabilities,
    SandboxCluster,
    SandboxError,
    SandboxImages,
    SandboxService,
    release_name,
    sandbox_namespace,
)

ROOT = Path(__file__).parent.parent.parent
PG = Image(
    uri="docker.io/library/postgres@sha256:" + "a" * 64, platforms=["linux/amd64", "linux/arm64"]
)
APP = Image(
    uri="ghcr.io/example/demo-api@sha256:" + "b" * 64, platforms=["linux/amd64", "linux/arm64"]
)
ENV_SECRET_VALUE = "env-secret-value-12345"
KUBECONFIG = Path("/tmp/cmp-sandbox-test.yaml")


def requirements(url_binding=False):
    bindings = {
        "DB_HOST": {"resource_id": "orders-db", "output": "host"},
        "DB_PASSWORD": {"resource_id": "orders-db", "output": "password_secret_ref"},
    }
    if url_binding:
        bindings["DATABASE_URL"] = {
            "resource_id": "orders-db",
            "output": "connection_url_secret_ref",
        }
    return RequirementsV2.model_validate(
        {
            "contract_version": "2",
            "resources": [
                {
                    "id": "orders-api", "kind": "APPLICATION", "replicas": 1, "cpu_millicores": 250,
                    "memory_mib": 256, "port": 8080, "health_path": "/health", "env_bindings": bindings,
                },
                {
                    "id": "orders-db", "kind": "POSTGRESQL", "version": "16", "extensions": [],
                    "database": {"name": "ordersdb", "username": "ordersuser"},
                    "cpu_millicores": 250, "memory_mib": 512, "storage_gib": 5,
                },
            ],
        }
    )  # fmt: skip


class FakeRunner:
    """Records every command; `existing` secrets answer `get secret`, everything else succeeds."""

    kubectl = "kubectl"
    helm = "helm"

    def __init__(self, existing=None, outputs=None):
        self.calls = []
        self.existing = dict(existing or {})
        self.outputs = outputs or {}

    def run(self, args, *, stdin=None, timeout=300, env=None):
        self.calls.append((list(args), stdin))
        text = " ".join(args)
        if "get secret" in text and "--ignore-not-found" in text:
            name = args[args.index("secret") + 1]
            if name in self.existing and "jsonpath" in text:
                return base64.b64encode(self.existing[name].encode())
            return b"secret/" + name.encode() if name in self.existing else b""
        for needle, out in self.outputs.items():
            if needle in text:
                return out
        return b""

    def commands(self, verb):
        return [a for a, _ in self.calls if verb in a]


def make_cluster(runner):
    return SandboxCluster(runner, kubeconfig=KUBECONFIG, context=SANDBOX_CONTEXT)


def make_service(runner, **kw):
    kw.setdefault("password_factory", lambda: "generated-db-password")
    return SandboxService(
        make_cluster(runner),
        database_chart_dir=ROOT / "infra/helm/postgres",
        application_chart_dir=ROOT / "infra/helm/application",
        **kw,
    )


def prepare(service, tmp_path, *, url=False, run_id="run1", env_names=("FEATURE_X",), now=None):
    env_file = tmp_path / ".env"
    env_file.write_text(f"FEATURE_X={ENV_SECRET_VALUE}\nOTHER=should-not-be-copied\n")
    return service.prepare_sandbox(
        requirements(url),
        migration_id="mig-1",
        run_id=run_id,
        images=SandboxImages(database=PG, applications={"orders-api": APP}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=list(env_names),
        env_file=env_file,
        now=now or datetime(2026, 11, 5, tzinfo=UTC),
    )


def stdin_of(runner, verb="create"):
    return [s for a, s in runner.calls if s is not None and verb in a]


def test_namespaces_and_releases_follow_a_deterministic_safe_rule():
    ns = sandbox_namespace("Mig_1 / Weird!", "Run-2")
    assert ns.startswith("cmp-sbx-") and len(ns) <= 63 and ns == ns.lower()
    assert ns == sandbox_namespace("Mig_1 / Weird!", "Run-2")
    assert ns != sandbox_namespace("Mig_1 / Weird!", "Run-3")
    assert len(sandbox_namespace("m" * 200, "r" * 200)) <= 63
    assert release_name("db", "Orders-DB") == "db-orders-db"
    assert len(release_name("app", "x" * 100)) <= 53


def test_only_the_dedicated_sandbox_context_is_accepted():
    with pytest.raises(SandboxError) as exc:
        SandboxCluster(FakeRunner(), kubeconfig=KUBECONFIG, context="minikube")
    assert exc.value.code == "CONTEXT_NOT_ALLOWED"
    SandboxCluster(FakeRunner(), kubeconfig=KUBECONFIG, context=SANDBOX_CONTEXT)


def test_prepare_creates_a_labelled_namespace_and_the_secrets_without_leaking_values(tmp_path):
    runner = FakeRunner()
    plan = prepare(make_service(runner), tmp_path, url=True)
    namespace_manifest = json.loads(stdin_of(runner, "apply")[0])
    labels = namespace_manifest["metadata"]["labels"]
    assert (
        labels["cmp.sandbox/run-id"] == "run1"
        and labels["app.kubernetes.io/managed-by"] == "cmp-sandbox"
    )
    assert labels["cmp.sandbox/expires-at"]
    secrets = [json.loads(s) for s in stdin_of(runner, "create")]
    by_name = {s["metadata"]["name"]: s["stringData"] for s in secrets}
    assert by_name[f"{plan.releases['orders-db']}-auth"] == {"password": "generated-db-password"}
    url = by_name[f"{plan.releases['orders-db']}-url"]["url"]
    assert (
        url
        == f"postgresql://ordersuser:generated-db-password@{plan.releases['orders-db']}.{plan.namespace}.svc:5432/ordersdb"
    )
    env = by_name[f"{plan.releases['orders-api']}-env"]
    assert env == {"FEATURE_X": ENV_SECRET_VALUE}  # only the names asked for, never OTHER
    flat = " ".join(" ".join(a) for a, _ in runner.calls)
    assert "generated-db-password" not in flat and ENV_SECRET_VALUE not in flat


def test_the_plan_and_every_result_hold_no_passwords_or_env_values(tmp_path):
    runner = FakeRunner()
    plan = prepare(make_service(runner), tmp_path, url=True)
    dumped = plan.model_dump_json()
    assert "generated-db-password" not in dumped and ENV_SECRET_VALUE not in dumped


def test_a_retry_of_the_same_run_does_not_recreate_the_database_secrets(tmp_path):
    first = FakeRunner()
    plan = prepare(make_service(first), tmp_path, url=True)
    db = plan.releases["orders-db"]
    retry = FakeRunner(
        existing={
            f"{db}-auth": "generated-db-password",
            f"{db}-url": "x",
            f"{plan.releases['orders-api']}-env": "x",
        }
    )
    calls = []
    service = make_service(retry, password_factory=lambda: calls.append(1) or "different-password")
    prepare(service, tmp_path, url=True)
    assert calls == [] and retry.commands("create") == []


def test_if_only_the_password_survived_the_url_secret_is_rebuilt_from_it(tmp_path):
    first = FakeRunner()
    plan = prepare(make_service(first), tmp_path, url=True)
    db = plan.releases["orders-db"]
    retry = FakeRunner(existing={f"{db}-auth": "kept-password"})
    prepare(make_service(retry, password_factory=lambda: "must-not-be-used"), tmp_path, url=True)
    urls = [json.loads(s)["stringData"]["url"] for s in stdin_of(retry, "create") if b"-url" in s]
    assert urls and ":kept-password@" in urls[0]


def test_the_request_is_valid_and_renders_with_the_shared_chart(tmp_path):
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2

    plan = prepare(make_service(FakeRunner()), tmp_path, url=True)
    SharedDeploymentRequestV2.model_validate(plan.request)
    result = render_shared_deployment_v2(
        plan.request,
        database_chart_dir=ROOT / "infra/helm/postgres",
        application_chart_dir=ROOT / "infra/helm/application",
    )
    app = next(r for r in result.resources if r.kind == "APPLICATION")
    refs = {n: v for n, v in app.resolved_env_bindings.items() if not isinstance(v, str)}
    assert refs["DATABASE_URL"].secret_name == f"{plan.releases['orders-db']}-url"
    assert refs["DB_PASSWORD"].secret_name == f"{plan.releases['orders-db']}-auth"
    assert refs["FEATURE_X"].secret_name == f"{plan.releases['orders-api']}-env"
    assert app.resolved_env_bindings["DB_HOST"].endswith(f".{plan.namespace}.svc")


def test_apply_installs_the_database_first_then_the_application_with_wait(tmp_path):
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    runner = FakeRunner()
    service = make_service(runner)
    plan = prepare(service, tmp_path)
    runner.calls.clear()
    service.apply_sandbox(plan)
    installs = runner.commands("upgrade")
    assert len(installs) == 2
    assert installs[0][installs[0].index("upgrade") + 2].endswith("postgres")
    assert installs[1][installs[1].index("upgrade") + 2].endswith("application")
    for command in installs:
        assert {"--install", "--wait", "--namespace", plan.namespace} <= set(command)
        assert command[command.index("--kube-context") + 1] == SANDBOX_CONTEXT
        values = (
            command[command.index("--values") + 1]
            if "--values" in command
            else command[command.index("-f") + 1]
        )
        assert not Path(values).exists()  # temporary values files are removed
    assert plan.releases["orders-db"] in installs[0] and plan.releases["orders-api"] in installs[1]


def test_readiness_checks_the_version_sql_and_the_application_health(tmp_path):
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    runner = FakeRunner(
        outputs={"select version()": b"PostgreSQL 16.4 on aarch64\n", "--raw": b"OK"}
    )
    service = make_service(runner)
    plan = prepare(service, tmp_path)
    report = service.check_sandbox(plan)
    assert report == {"database_version": "16", "sql": "OK", "health": "OK"}
    exec_args = " ".join(runner.commands("exec")[0])
    assert "generated-db-password" not in exec_args  # the pod reads its own Secret


def test_a_failed_health_check_raises_a_code_only_error(tmp_path):
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    from cmp_worker.activities.k8s_workloads_cli import CommandError

    class Failing(FakeRunner):
        def run(self, args, **kw):
            if "--raw" in args:
                raise CommandError("kubectl")
            return super().run(args, **kw)

    runner = Failing(outputs={"select version()": b"PostgreSQL 16.4\n"})
    service = make_service(runner)
    plan = prepare(service, tmp_path)
    with pytest.raises(SandboxError) as exc:
        service.check_sandbox(plan)
    assert exc.value.code == "HEALTH_CHECK_FAILED"


def test_teardown_deletes_the_namespace_and_waits(tmp_path):
    runner = FakeRunner()
    service = make_service(runner)
    plan = prepare(service, tmp_path)
    runner.calls.clear()
    service.teardown_sandbox(plan.namespace)
    delete = runner.commands("delete")[0]
    assert delete[delete.index("delete") + 1] == "namespace" and plan.namespace in delete
    assert "--wait=true" in delete and "--ignore-not-found" in delete


def test_teardown_refuses_namespaces_that_are_not_sandboxes():
    service = make_service(FakeRunner())
    for name in ("default", "kube-system", "cmp-prod-demo", "cmp-sbx-"):
        with pytest.raises(SandboxError) as exc:
            service.teardown_sandbox(name)
        assert exc.value.code == "NOT_A_SANDBOX_NAMESPACE"


def test_expired_sandboxes_are_deleted_and_current_ones_are_kept():
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    listing = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "cmp-sbx-old",
                        "labels": {
                            "cmp.sandbox/expires-at": str(
                                int((now - timedelta(hours=1)).timestamp())
                            )
                        },
                    }
                },
                {
                    "metadata": {
                        "name": "cmp-sbx-new",
                        "labels": {
                            "cmp.sandbox/expires-at": str(
                                int((now + timedelta(hours=1)).timestamp())
                            )
                        },
                    }
                },
                {"metadata": {"name": "cmp-sbx-unlabelled", "labels": {}}},
            ]
        }
    ).encode()
    runner = FakeRunner(outputs={"get namespaces": listing})
    deleted = make_service(runner).cleanup_expired(now=now)
    assert deleted == ["cmp-sbx-old"]
    assert [c[c.index("delete") + 2] for c in runner.commands("delete")] == ["cmp-sbx-old"]
