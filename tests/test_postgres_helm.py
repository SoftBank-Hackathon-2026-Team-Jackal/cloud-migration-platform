"""Real Helm output is the public seam; no cluster or cloud credentials are used."""

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from cmp_contracts.chart import PostgresChartValues

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "infra/helm/postgres"
HELM = os.environ.get("HELM_BIN", "helm")


@pytest.fixture
def values():
    return json.loads((ROOT / "tests/fixtures/contracts/postgres-values.json").read_text())


def helm(tmp_path, values, command="template"):
    path = tmp_path / "values.json"
    path.write_text(json.dumps(values))
    args = [HELM, command]
    if command == "template":
        args += [values["release_name"]]
    args += [str(CHART), "--namespace", values["namespace"], "-f", str(path)]
    return subprocess.run(args, capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    "provider,storage", [("AWS", "gp3"), ("GCP", "standard-rwo"), ("ON_PREM", "local-path")]
)
@pytest.mark.parametrize("environment", ["SANDBOX", "PRODUCTION"])
def test_render_resources(tmp_path, values, provider, storage, environment):
    values.update(provider=provider, environment=environment)
    values["storage"]["class_name"] = storage
    PostgresChartValues.model_validate(values)
    lint = helm(tmp_path, values, "lint")
    assert lint.returncode == 0, lint.stderr + lint.stdout
    result = helm(tmp_path, values)
    assert result.returncode == 0, result.stderr
    docs = {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout) if doc}
    assert set(docs) == {"StatefulSet", "Service", "PersistentVolumeClaim"}
    name = values["release_name"]
    assert all(doc["metadata"]["namespace"] == values["namespace"] for doc in docs.values())
    sts = docs["StatefulSet"]["spec"]
    assert sts["replicas"] == 1
    assert sts["serviceName"] == name
    assert sts["selector"]["matchLabels"] == docs["Service"]["spec"]["selector"]
    assert sts["template"]["metadata"]["labels"] == sts["selector"]["matchLabels"]
    pod = sts["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == values["image"]["uri"]
    assert container["resources"] == {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }
    env = {e["name"]: e for e in container["env"]}
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": values["database"]["secret_name"],
        "key": "password",
    }
    assert env["POSTGRES_DB"]["value"] == values["database"]["name"]
    assert env["PGDATA"]["value"] == "/var/lib/postgresql/data/pgdata"
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == name
    pvc = docs["PersistentVolumeClaim"]["spec"]
    assert pvc["storageClassName"] == storage
    assert pvc["resources"]["requests"]["storage"] == "20Gi"
    assert pvc["accessModes"] == ["ReadWriteOnce"]
    assert docs["Service"]["spec"]["ports"][0]["port"] == 5432
    assert "Secret" not in docs


@pytest.mark.parametrize(
    "section,key,bad",
    [
        ("storage", "class_name", ""),
        ("storage", "size_gib", 0),
        ("storage", "size_gib", "20Gi"),
        ("database", "secret_name", "bad/name"),
        ("database", "secret_key", ""),
        ("database", "version", "17"),
        ("resources", "cpu_millicores", -1),
        ("resources", "memory_mib", "512"),
        ("image", "uri", "postgres:16"),
        ("database", "password", "must-not-render"),
        ("image", "registry_auth_ref", {"id": "opaque-reference", "version": "1"}),
    ],
)
def test_invalid_values_fail_before_render(tmp_path, values, section, key, bad):
    values[section][key] = bad
    result = helm(tmp_path, values)
    assert result.returncode != 0
    assert "kind: StatefulSet" not in result.stdout
    assert "schema" in result.stderr


def test_missing_reference_fails(tmp_path, values):
    del values["database"]["secret_name"]
    assert helm(tmp_path, values).returncode != 0


def test_release_namespace_mismatch_fails(tmp_path, values):
    path = tmp_path / "values.json"
    path.write_text(json.dumps(values))
    result = subprocess.run(
        [HELM, "template", "different", str(CHART), "-n", values["namespace"], "-f", str(path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_values_schema_matches_shared_model(values):
    schema = json.loads((CHART / "values.schema.json").read_text())
    expected = copy.deepcopy(PostgresChartValues.model_json_schema())
    expected["$defs"]["Image"]["properties"]["registry_auth_ref"] = {
        "type": "null",
        "default": None,
        "description": "Registry credential resolution is not implemented in ticket 02.",
    }
    assert schema == expected


def test_custom_units_architecture_and_yaml_strings(tmp_path, values):
    values["resources"] = {"cpu_millicores": 1250, "memory_mib": 2048}
    values["storage"]["size_gib"] = 75
    values["database"]["name"] = 'true: "quoted"'
    values["database"]["username"] = "false"
    values["image"]["platforms"] = ["linux/arm64"]
    result = helm(tmp_path, values)
    assert result.returncode == 0, result.stderr
    docs = {d["kind"]: d for d in yaml.safe_load_all(result.stdout) if d}
    pod = docs["StatefulSet"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["resources"]["requests"] == {"cpu": "1250m", "memory": "2048Mi"}
    assert container["env"][0]["value"] == values["database"]["name"]
    assert container["env"][1]["value"] == "false"
    expressions = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ][0]["matchExpressions"]
    assert expressions[0]["values"] == ["arm64"]
    assert docs["PersistentVolumeClaim"]["spec"]["resources"]["requests"]["storage"] == "75Gi"
    assert (
        docs["PersistentVolumeClaim"]["metadata"]["annotations"]["helm.sh/resource-policy"]
        == "keep"
    )
