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


def prepare(service, run_id, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"FEATURE_X={ENV_VALUE}\nAWS_REGION=ap-northeast-2\n")
    return service.prepare_sandbox(
        requirements(),
        migration_id="it-mig",
        run_id=run_id,
        images=SandboxImages(database=POSTGRES, applications={"demo-api": PODINFO}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=["FEATURE_X"],  # AWS_REGION is an excluded variable and is never copied
        env_file=env_file,
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
