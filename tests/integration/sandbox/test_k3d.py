"""Real Sandbox on the dedicated `cmp-sandbox` k3d cluster. Opt in with:

  CMP_SANDBOX_KUBECONFIG=~/.kube/cmp-sandbox.yaml uv run pytest tests/integration/sandbox -m k3d

Only the `k3d-cmp-sandbox` context is ever used; any other context is refused.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from cmp_contracts.provisioning import Image
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_worker.activities.k8s_workloads_cli import CommandRunner
from cmp_worker.sandbox_deploy import (
    SANDBOX_CONTEXT,
    SandboxCapabilities,
    SandboxCluster,
    SandboxImages,
    SandboxService,
)

ROOT = Path(__file__).parent.parent.parent.parent
KUBECONFIG = os.environ.get("CMP_SANDBOX_KUBECONFIG")
pytestmark = [
    pytest.mark.k3d,
    pytest.mark.skipif(not KUBECONFIG, reason="set CMP_SANDBOX_KUBECONFIG to run the k3d tests"),
]

# Public images pinned by digest (resolved from the registries on 2026-11-05).
POSTGRES = Image(
    uri="docker.io/library/postgres@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a",
    platforms=["linux/amd64", "linux/arm64"],
)
PODINFO = Image(
    uri="ghcr.io/stefanprodan/podinfo@sha256:862ca45e61b32392f7941a1bdfdbe5ff8b6899070135f1bdca1c287d0057fc94",
    platforms=["linux/amd64", "linux/arm64"],
)
PODINFO_OLD = Image(
    uri="ghcr.io/stefanprodan/podinfo@sha256:d2b3cd93a48acdc91327533ce28fcb3169b2d9feaf73817dc2eb68858df64edb",
    platforms=["linux/amd64", "linux/arm64"],
)
ENV_VALUE = "k3d-env-secret-value-7788"


def requirements():
    return RequirementsV2.model_validate(
        {
            "contract_version": "2",
            "resources": [
                {
                    "id": "demo-api", "kind": "APPLICATION", "replicas": 1, "cpu_millicores": 100,
                    "memory_mib": 128, "port": 9898, "health_path": "/healthz",
                    "env_bindings": {
                        "DB_HOST": {"resource_id": "demo-db", "output": "host"},
                        "DB_PASSWORD": {"resource_id": "demo-db", "output": "password_secret_ref"},
                        "DATABASE_URL": {"resource_id": "demo-db", "output": "connection_url_secret_ref"},
                    },
                },
                {
                    "id": "demo-db", "kind": "POSTGRESQL", "version": "16", "extensions": [],
                    "database": {"name": "demoapp", "username": "demoapp"},
                    "cpu_millicores": 250, "memory_mib": 512, "storage_gib": 1,
                },
            ],
        }
    )  # fmt: skip


def kubectl(*args):
    return subprocess.run(
        ["kubectl", "--kubeconfig", str(Path(KUBECONFIG).expanduser()), "--context", SANDBOX_CONTEXT, *args],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


@pytest.fixture
def service():
    cluster = SandboxCluster(
        CommandRunner(), kubeconfig=Path(KUBECONFIG).expanduser(), context=SANDBOX_CONTEXT
    )
    return SandboxService(
        cluster,
        database_chart_dir=ROOT / "infra/helm/postgres",
        application_chart_dir=ROOT / "infra/helm/application",
    )


def prepare(service, run_id, tmp_path, *, app=PODINFO, env_value=ENV_VALUE, **kw):
    env_file = tmp_path / ".env"
    env_file.write_text(f"FEATURE_X={env_value}\nAWS_REGION=ap-northeast-2\n")
    return service.prepare_sandbox(
        requirements(),
        migration_id="it-mig",
        run_id=run_id,
        images=SandboxImages(database=POSTGRES, applications={"demo-api": app}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=["FEATURE_X"],  # AWS_REGION is an excluded variable and is never copied
        env_file=env_file,
        **kw,
    )


def test_a_sandbox_comes_up_passes_its_checks_and_is_removed_completely(service, tmp_path):
    run_id = "it-" + uuid.uuid4().hex[:8]
    plan = prepare(service, run_id, tmp_path)
    try:
        password_uid = kubectl(
            "get",
            "secret",
            f"{plan.releases['demo-db']}-auth",
            "-n",
            plan.namespace,
            "-o",
            "jsonpath={.metadata.uid}",
        ).stdout
        assert password_uid
        deployment = service.apply_sandbox(plan)
        assert set(deployment["endpoints"]) == {"demo-api", "demo-db"}
        report = service.check_sandbox(plan)
        assert report == {"database_version": "16", "sql": "OK", "health": "OK"}

        # The app's own Secret holds only the requested name, and the URL Secret exists.
        env_secret = json.loads(
            kubectl(
                "get",
                "secret",
                f"{plan.releases['demo-api']}-env",
                "-n",
                plan.namespace,
                "-o",
                "json",
            ).stdout
        )
        assert list(env_secret["data"]) == ["FEATURE_X"]
        assert kubectl(
            "get", "secret", f"{plan.releases['demo-db']}-url", "-n", plan.namespace, "-o", "name"
        ).stdout.strip()

        # A retry of the same run must not recreate the password Secret.
        prepare(service, run_id, tmp_path)
        again = kubectl(
            "get",
            "secret",
            f"{plan.releases['demo-db']}-auth",
            "-n",
            plan.namespace,
            "-o",
            "jsonpath={.metadata.uid}",
        ).stdout
        assert again == password_uid

        # No secret value leaks into what the worker hands back.
        handed_back = json.dumps([plan.model_dump(mode="json"), deployment, report])
        assert ENV_VALUE not in handed_back
    finally:
        service.teardown_sandbox(plan.namespace)

    assert (
        kubectl(
            "get", "namespace", plan.namespace, "--ignore-not-found", "-o", "name"
        ).stdout.strip()
        == ""
    )
    leftovers = kubectl("get", "pvc", "-A", "-o", "json")
    assert plan.namespace not in leftovers.stdout


def test_the_dedicated_context_is_the_only_one_the_cluster_accepts():
    from cmp_worker.sandbox_deploy import SandboxError

    with pytest.raises(SandboxError):
        SandboxCluster(
            CommandRunner(), kubeconfig=Path(KUBECONFIG).expanduser(), context="minikube"
        )


def jsonpath(*args):
    return kubectl(*args).stdout.strip()


def sql(plan, statement):
    release = plan.releases["demo-db"]
    script = f'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "{statement}"'
    return kubectl("exec", "-n", plan.namespace, f"statefulset/{release}", "--", "sh", "-c", script)


def test_update_changes_only_what_differs_and_reset_restores_the_baseline(service, tmp_path):
    run_id = "up-" + uuid.uuid4().hex[:8]
    first = prepare(service, run_id, tmp_path, app=PODINFO_OLD)
    try:
        service.apply_sandbox(first)
        service.check_sandbox(first)
        ns, db, app = first.namespace, first.releases["demo-db"], first.releases["demo-api"]
        db_pod = jsonpath("get", "pod", f"{db}-0", "-n", ns, "-o", "jsonpath={.metadata.uid}")
        db_rev = jsonpath("get", "secret", "-n", ns, "-l", f"name={db},owner=helm", "-o", "name")
        assert db_pod

        # Baseline first, then data the test run is allowed to change.
        assert service.snapshot_baseline(first) == {"baseline": "SAVED"}
        assert sql(first, "create table scratch(id int)").returncode == 0

        # Image-only change: the database release and pod are untouched.
        second = prepare(service, run_id, tmp_path, app=PODINFO, update_env=True)
        record = service.update_sandbox(first, second, attempt=2)
        assert {k: v.action for k, v in record.resources.items()} == {
            "demo-api": "UPGRADED",
            "demo-db": "UNCHANGED",
        }
        assert record.baseline == "KEPT"
        assert record.resources["demo-api"].image_digest == PODINFO.uri.rsplit("@", 1)[1]
        assert (
            jsonpath("get", "pod", f"{db}-0", "-n", ns, "-o", "jsonpath={.metadata.uid}") == db_pod
        )
        assert (
            jsonpath("get", "secret", "-n", ns, "-l", f"name={db},owner=helm", "-o", "name")
            == db_rev
        )
        service.check_sandbox(second)

        # Changed env value: the application pod is restarted, the database is not.
        app_pods = jsonpath(
            "get", "pod", "-n", ns, "-l", f"app.kubernetes.io/instance={app}", "-o", "name"
        )
        third = prepare(
            service, run_id, tmp_path, app=PODINFO, env_value="rotated-7799", update_env=True
        )
        assert third.env_updated == [f"{app}-env"]
        record = service.update_sandbox(second, third, attempt=3)
        assert record.resources["demo-api"].action == "RESTARTED"
        assert (
            jsonpath(
                "get", "pod", "-n", ns, "-l", f"app.kubernetes.io/instance={app}", "-o", "name"
            )
            != app_pods
        )
        assert (
            jsonpath("get", "pod", f"{db}-0", "-n", ns, "-o", "jsonpath={.metadata.uid}") == db_pod
        )
        assert "rotated-7799" not in json.dumps(
            [third.model_dump(mode="json"), record.model_dump(mode="json")]
        )

        # Reset removes what the run created and keeps what the baseline held.
        assert sql(third, "select count(*) from scratch").stdout.strip() == "0"
        assert service.reset_test_data(third) == {"reset": "OK"}
        assert "does not exist" in sql(third, "select count(*) from scratch").stderr
        service.check_sandbox(third)
    finally:
        service.teardown_sandbox(first.namespace)


def test_a_pinned_suite_runs_in_the_sandbox_and_dependency_checks_use_the_real_database(
    service, tmp_path
):
    from cmp_testsuite import (
        ActionStep,
        CaseResult,
        Expect,
        HttpStep,
        Suite,
        SuiteCase,
        SuiteError,
        dump_bundle,
        run_case,
        run_suite,
    )
    from cmp_testsuite.standard import AppApi, standard_cases
    from cmp_testsuite.validate import validate_tests
    from cmp_worker.artifact_store import LocalArtifactStore

    run_id = "st-" + uuid.uuid4().hex[:8]
    plan = prepare(service, run_id, tmp_path)
    store = LocalArtifactStore(tmp_path / "suite-store")
    try:
        service.apply_sandbox(plan)
        service.check_sandbox(plan)
        survives = SuiteCase(
            id="app-up-after-db-restart", source="STANDARD", effect="READ_ONLY",
            steps=[ActionStep(action="RESTART_DATABASE"), HttpStep(path="/healthz", expect=Expect(status=200))],
        )  # fmt: skip
        info = SuiteCase(
            id="ai-info", source="AI", effect="READ_ONLY",
            steps=[HttpStep(path="/", expect=Expect(status=200, body_contains="podinfo"))],
        )  # fmt: skip
        suite = Suite(
            name="demo", cases=[*standard_cases(AppApi(health_path="/healthz")), survives]
        )
        ref = store.put(dump_bundle(suite), ref_id="suite", kind="test-suite")
        actions = {"RESTART_DATABASE": lambda: service.restart_database(plan)}
        with service.cluster.proxy() as proxy:
            base = proxy + service.service_path(plan, "demo-api")
            result = run_suite(ref, base, store=store, actions=actions)
            assert result.passed, result.results
            assert {r.id: r.status for r in result.results} == {
                "std-health": "PASSED",
                "app-up-after-db-restart": "PASSED",
            }

            # The same pinned suite refuses to run once its bytes change.
            (store.root / ref.sha256).write_bytes(dump_bundle(Suite(name="demo", cases=[info])))
            with pytest.raises(SuiteError):
                run_suite(ref, base, store=store)

            # podinfo does not use the database, so cutting it must not fail the test: refused.
            def target(case):
                return run_case(case, base)

            verdict = validate_tests(
                [info],
                target=target,
                origin=lambda case: CaseResult(id=case.id, status="PASSED"),
                cut_dependency=lambda: service.database_cut(plan),
            ).verdicts[0]
            assert (verdict.accepted, verdict.reason) == (False, "PASSES_WITHOUT_DEPENDENCY")
        service.check_sandbox(plan)  # the database is back after the cut
    finally:
        service.teardown_sandbox(plan.namespace)
