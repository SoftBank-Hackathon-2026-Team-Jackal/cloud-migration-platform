"""Public Sandbox/ON_PREM rendering seam; no cluster Apply is performed."""

import json
from pathlib import Path

import pytest
import yaml
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.shared_deployment import render_shared_deployment

ROOT = Path(__file__).resolve().parents[1]


def request_payload():
    app = json.loads((ROOT / "tests/fixtures/contracts/app-values.json").read_text())
    db = json.loads((ROOT / "tests/fixtures/contracts/postgres-values.json").read_text())
    return {
        "contract_version": "1",
        "environment": "SANDBOX",
        "provider": "ON_PREM",
        "namespace": "cmp-test-001",
        "release_names": {"app": "cmp-test-001-app", "db": "cmp-test-001-db"},
        "storage_class": "local-path",
        "app": {
            "image": app["image"],
            "config_ref": app["config_ref"],
            "replicas": app["application"]["replicas"],
            "cpu_millicores": app["application"]["cpu_millicores"],
            "memory_mib": app["application"]["memory_mib"],
            "port": app["application"]["port"],
            "health_path": app["application"]["health_path"],
        },
        "db": {
            "image": db["image"],
            "version": "16",
            "name": db["database"]["name"],
            "username": db["database"]["username"],
            "password_secret_name": db["database"]["secret_name"],
            "password_secret_key": db["database"]["secret_key"],
            "cpu_millicores": db["resources"]["cpu_millicores"],
            "memory_mib": db["resources"]["memory_mib"],
            "pvc_size_gib": db["storage"]["size_gib"],
        },
        "env_bindings": {
            item["name"]: (
                "database." + item["source"]["field"]
                if item["source"]["kind"] == "DATABASE"
                else item["source"]
            )
            for item in app["env"]
        },
    }


@pytest.mark.parametrize("environment", ["SANDBOX", "PRODUCTION"])
def test_shared_render_returns_deployable_artifacts_and_references(environment):
    payload = request_payload()
    payload["environment"] = environment
    result = render_shared_deployment(
        payload,
        database_chart_dir=ROOT / "infra/helm/postgres",
        application_chart_dir=ROOT / "infra/helm/application",
    )
    assert result.environment == environment
    assert result.database.service.host == "cmp-test-001-db.cmp-test-001.svc"
    assert result.database.service.port == 5432
    assert result.bindings["DB_HOST"] == result.database.service.host
    assert result.bindings["DB_PASSWORD"].secret_name == "sandbox-db-auth"
    assert result.bindings["DB_PASSWORD"].secret_key == "password"
    assert result.database.helm_values.storage.class_name == "local-path"
    assert result.application.helm_values.database.host == result.database.service.host
    db_docs = {doc["kind"] for doc in yaml.safe_load_all(result.database.manifest_yaml) if doc}
    app_docs = {doc["kind"] for doc in yaml.safe_load_all(result.application.manifest_yaml) if doc}
    assert db_docs == {"StatefulSet", "Service", "PersistentVolumeClaim"}
    assert app_docs == {"Deployment", "Service"}
    assert result.validation.passed is True


@pytest.mark.parametrize("field", ["image", "password_secret_name", "storage_class"])
def test_shared_render_rejects_missing_required_field(field):
    payload = request_payload()
    if field == "image":
        del payload["app"][field]
    elif field == "password_secret_name":
        del payload["db"][field]
    else:
        del payload[field]
    with pytest.raises(ProvisioningInputError):
        render_shared_deployment(
            payload,
            database_chart_dir=ROOT / "infra/helm/postgres",
            application_chart_dir=ROOT / "infra/helm/application",
        )


def test_shared_render_rejects_raw_secret_without_echo(capsys):
    marker = "DO_NOT_DISCLOSE_SECRET_987654"
    payload = request_payload()
    payload["db"]["password"] = marker
    with pytest.raises(ProvisioningInputError) as caught:
        render_shared_deployment(
            payload,
            database_chart_dir=ROOT / "infra/helm/postgres",
            application_chart_dir=ROOT / "infra/helm/application",
        )
    assert marker not in str(caught.value)
    assert marker not in capsys.readouterr().out
