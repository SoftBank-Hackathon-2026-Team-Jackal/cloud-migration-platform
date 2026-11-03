"""Resource graph → shared Helm render output, without cluster access."""

from copy import deepcopy
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2

ROOT = Path(__file__).resolve().parents[1]
DB_CHART = ROOT / "infra/helm/postgres"
APP_CHART = ROOT / "infra/helm/application"
APP_DIGEST = "sha256:" + "a" * 64
DB_DIGEST = "sha256:" + "b" * 64


def request_payload():
    return {
        "contract_version": "2",
        "environment": "SANDBOX",
        "provider": "ON_PREM",
        "namespace": "cmp-test-001",
        "resources": [
            {
                "id": "postgres-main",
                "kind": "POSTGRESQL",
                "release_name": "cmp-test-001-db",
                "image": {
                    "uri": f"docker.io/library/postgres@{DB_DIGEST}",
                    "platforms": ["linux/amd64"],
                },
                "version": "16",
                "extensions": [],
                "database": {
                    "name": "appdb",
                    "username": "appuser",
                    "password_secret_ref": {"name": "db-auth", "key": "password"},
                },
                "resources": {"cpu_millicores": 250, "memory_mib": 512},
                "storage": {"class_name": "local-path", "size_gib": 5},
            },
            {
                "id": "api-main",
                "kind": "APPLICATION",
                "release_name": "cmp-test-001-app",
                "image": {
                    "uri": f"123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/demo@{APP_DIGEST}",
                    "platforms": ["linux/amd64"],
                },
                "replicas": 1,
                "resources": {"cpu_millicores": 250, "memory_mib": 256},
                "port": 8080,
                "health_path": "/health",
                "env_bindings": {
                    "DB_HOST": {"resource_id": "postgres-main", "output": "host"},
                    "DB_PORT": {"resource_id": "postgres-main", "output": "port"},
                    "DB_PASSWORD": {
                        "resource_id": "postgres-main",
                        "output": "password_secret_ref",
                    },
                    "API_KEY": {"secret_ref": {"name": "app-env", "key": "API_KEY"}},
                },
            },
        ],
    }


def render(payload):
    return render_shared_deployment_v2(
        payload,
        database_chart_dir=DB_CHART,
        application_chart_dir=APP_CHART,
    )


def docs(manifest):
    return [item for item in yaml.safe_load_all(manifest) if item]


def test_v2_renders_database_then_application_with_final_references():
    result = render(request_payload())
    assert [item.id for item in result.resources] == ["postgres-main", "api-main"]
    database, application = result.resources
    assert database.outputs.host == "cmp-test-001-db.cmp-test-001.svc"
    assert database.outputs.password_secret_ref.name == "db-auth"
    assert application.resolved_env_bindings["DB_HOST"] == database.outputs.host
    assert application.resolved_env_bindings["DB_PORT"] == "5432"
    assert application.resolved_env_bindings["DB_PASSWORD"].secret_name == "db-auth"
    assert application.resolved_env_bindings["API_KEY"].secret_key == "API_KEY"
    assert {item["kind"] for item in docs(database.manifest_yaml)} == {
        "Service",
        "PersistentVolumeClaim",
        "StatefulSet",
    }
    deployment = next(
        item for item in docs(application.manifest_yaml) if item["kind"] == "Deployment"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["DB_HOST"]["value"] == database.outputs.host
    assert env["DB_PORT"]["value"] == "5432"
    assert env["DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "db-auth", "key": "password"}
    assert env["API_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "app-env", "key": "API_KEY"}
    assert container["image"].endswith(APP_DIGEST)
    assert result.validation.passed is True


def test_multiple_apps_can_bind_different_postgresql_resources():
    payload = request_payload()
    second_db = deepcopy(payload["resources"][0])
    second_db["id"] = "postgres-analytics"
    second_db["release_name"] = "cmp-test-001-analytics-db"
    second_db["database"]["name"] = "analytics"
    second_app = deepcopy(payload["resources"][1])
    second_app["id"] = "worker-main"
    second_app["release_name"] = "cmp-test-001-worker"
    second_app["env_bindings"]["DB_HOST"]["resource_id"] = "postgres-analytics"
    second_app["env_bindings"]["DB_PASSWORD"]["resource_id"] = "postgres-analytics"
    payload["resources"] += [second_db, second_app]
    result = render(payload)
    resources = {item.id: item for item in result.resources}
    assert resources["worker-main"].resolved_env_bindings["DB_HOST"] == (
        "cmp-test-001-analytics-db.cmp-test-001.svc"
    )
    assert resources["api-main"].resolved_env_bindings["DB_HOST"] == (
        "cmp-test-001-db.cmp-test-001.svc"
    )
    assert len({item.release_name for item in result.resources}) == 4


@pytest.mark.parametrize(
    "environment,provider",
    [
        ("SANDBOX", "ON_PREM"),
        ("PRODUCTION", "ON_PREM"),
        ("PRODUCTION", "AWS"),
        ("PRODUCTION", "GCP"),
    ],
)
def test_same_renderer_accepts_each_target_context(environment, provider):
    payload = request_payload()
    payload["environment"] = environment
    payload["provider"] = provider
    result = render(payload)
    assert result.environment == environment
    assert result.provider == provider
    assert all(item.helm_values.provider == provider for item in result.resources)


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda p: p["resources"][1].update(id="postgres-main"), "DUPLICATE_RESOURCE_ID"),
        (lambda p: p["resources"][1].update(release_name="cmp-test-001-db"), "DUPLICATE_RELEASE"),
        (
            lambda p: p["resources"][1]["env_bindings"]["DB_HOST"].update(resource_id="missing"),
            "UNKNOWN_RESOURCE_REF",
        ),
        (
            lambda p: p["resources"][1]["env_bindings"]["DB_HOST"].update(resource_id="api-main"),
            "UNSUPPORTED_RESOURCE_OUTPUT",
        ),
        (lambda p: p["resources"][0].update(extensions=["postgis"]), "UNSUPPORTED_DB_EXTENSION"),
        (lambda p: p["resources"][1].update(kind="REDIS"), "INVALID_DEPLOYMENT_INPUT"),
        (
            lambda p: p["resources"][1]["image"].update(uri="demo:latest"),
            "INVALID_DEPLOYMENT_INPUT",
        ),
        (
            lambda p: p["resources"][1]["image"].update(platforms=["linux/ppc64le"]),
            "INVALID_DEPLOYMENT_INPUT",
        ),
    ],
)
def test_invalid_resource_graph_is_rejected(change, code):
    payload = request_payload()
    change(payload)
    with pytest.raises(ProvisioningInputError) as caught:
        render(payload)
    assert code in {issue.code for issue in caught.value.issues}


def test_raw_secret_is_rejected_without_echo():
    payload = request_payload()
    marker = "DO_NOT_DISCLOSE_THIS_PASSWORD"
    payload["resources"][0]["database"]["password"] = marker
    with pytest.raises(ProvisioningInputError) as caught:
        render(payload)
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value.issues)


def test_helm_error_does_not_expose_stderr(monkeypatch):
    marker = "DO_NOT_DISCLOSE_HELM_STDERR"

    def failed_helm(command, **_kwargs):
        return CompletedProcess(command, 1, stderr=marker.encode())

    monkeypatch.setattr("cmp_providers.shared_deployment_v2.subprocess.run", failed_helm)
    with pytest.raises(ProvisioningInputError) as caught:
        render(request_payload())
    assert "DEPLOYMENT_RENDER_FAILED" in {issue.code for issue in caught.value.issues}
    assert marker not in str(caught.value)
