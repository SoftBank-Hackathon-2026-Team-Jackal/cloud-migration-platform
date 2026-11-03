import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from cmp_contracts.application import ApplicationValues
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.app_renderer import ApplicationRenderError, render_application
from cmp_providers.application import resolve_application, verify_application

CHART = Path(__file__).resolve().parents[1] / "infra/helm/application"
HELM = os.environ.get("HELM_BIN", "helm")

from cmp_providers.application import prepare_application


def test_explicit_mapping_plan():
    payload = json.loads((Path(__file__).parent / "fixtures/contracts/app-values.json").read_text())
    result = prepare_application(payload)
    assert [rule.target for rule in result.bindings] == ["database.host", "database.secret_name"]
    assert result.values.database.host is None
    assert result.values.database.secret_name is None
    assert result.values.config_ref.id == "uploaded-env"


def test_real_render():

    payload = json.loads((Path(__file__).parent / "fixtures/contracts/app-values.json").read_text())
    bound = resolve_application(
        payload, {"DATABASE_HOST": "db.sandbox-001.svc", "DATABASE_SECRET_NAME": "db-auth"}
    )
    output = render_application(
        bound, chart_dir=Path("infra/helm/application"), helm_bin=os.environ.get("HELM_BIN", "helm")
    )
    docs = {d["kind"]: d for d in yaml.safe_load_all(output) if d}
    assert set(docs) == {"Deployment", "Service"}
    container = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    assert env["DB_HOST"]["value"] == "db.sandbox-001.svc"
    assert env["DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "db-auth", "key": "password"}


@pytest.fixture
def payload():
    return json.loads((Path(__file__).parent / "fixtures/contracts/app-values.json").read_text())


@pytest.fixture
def resolved():
    return {"DATABASE_HOST": "db.target.svc", "DATABASE_SECRET_NAME": "generated-db-auth"}


@pytest.mark.parametrize("provider", ["AWS", "GCP", "ON_PREM"])
@pytest.mark.parametrize("environment", ["SANDBOX", "PRODUCTION"])
def test_target_render_and_lint(tmp_path, payload, resolved, provider, environment):
    payload.update(provider=provider, environment=environment)
    bound = resolve_application(payload, resolved)
    verify_application(payload, bound, resolved)
    path = tmp_path / "values.json"
    path.write_text(json.dumps(bound))
    lint = subprocess.run(
        [HELM, "lint", str(CHART), "-f", str(path)], capture_output=True, check=False
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr
    docs = {
        d["kind"]: d
        for d in yaml.safe_load_all(render_application(bound, chart_dir=CHART, helm_bin=HELM))
        if d
    }
    deployment = docs["Deployment"]["spec"]
    assert deployment["replicas"] == 2
    assert deployment["selector"]["matchLabels"] == docs["Service"]["spec"]["selector"]
    assert deployment["template"]["metadata"]["labels"] == deployment["selector"]["matchLabels"]
    container = deployment["template"]["spec"]["containers"][0]
    assert container["image"] == payload["image"]["uri"]
    assert container["ports"][0]["containerPort"] == 8080
    assert container["readinessProbe"]["httpGet"] == {"path": "/health", "port": "http"}
    assert container["resources"]["requests"] == {"cpu": "500m", "memory": "512Mi"}
    env = {e["name"]: e for e in container["env"]}
    assert env["DB_PORT"]["value"] == "5432"
    assert env["DB_NAME"]["value"] == "demo"
    assert env["DB_USER"]["value"] == "app_user"
    assert env["API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "uploaded-secrets",
        "key": "api-key",
    }
    assert env["APP_MODE"]["valueFrom"]["configMapKeyRef"] == {"name": "app-config", "key": "mode"}
    assert payload["database"]["host"] is None  # input remains immutable
    assert set(docs) == {"Deployment", "Service"}


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "unknown", "raw", "invalid_ref", "prefilled", "registry"]
)
def test_invalid_metadata_is_safe(payload, mutation, capsys):
    marker = "DO_NOT_DISCLOSE_PASSWORD_012345"
    if mutation == "missing":
        payload["env"].pop()
    elif mutation == "duplicate":
        payload["env"].append(copy.deepcopy(payload["env"][0]))
    elif mutation == "unknown":
        payload["env"][0]["source"] = {"kind": marker}
    elif mutation == "raw":
        payload[marker] = marker
        payload["env"][0]["value"] = marker
    elif mutation == "invalid_ref":
        payload["env"][0]["source"]["name"] = marker
    elif mutation == "prefilled":
        payload["database"]["host"] = marker
    elif mutation == "registry":
        payload["image"]["registry_auth_ref"] = {"id": marker, "version": "1"}
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_application(payload)
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value.issues)
    output = capsys.readouterr()
    assert marker not in output.out + output.err


@pytest.mark.parametrize("mutation", ["missing", "extra", "host", "secret"])
def test_bad_resolved_reference_is_safe(payload, resolved, mutation):
    marker = "DO_NOT_DISCLOSE_PASSWORD_012345"
    if mutation == "missing":
        resolved.pop("DATABASE_HOST")
    elif mutation == "extra":
        resolved[marker] = marker
    elif mutation == "host":
        resolved["DATABASE_HOST"] = "https://" + marker
    else:
        resolved["DATABASE_SECRET_NAME"] = marker
    with pytest.raises(ProvisioningInputError) as caught:
        resolve_application(payload, resolved)
    assert marker not in str(caught.value)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("application", "replicas", 9),
        ("database", "port", 9999),
        ("database", "secret_name", "other-secret"),
        ("image", "uri", "other@sha256:" + "c" * 64),
    ],
)
def test_nonapproved_changes_rejected(payload, resolved, section, key, value):
    bound = resolve_application(payload, resolved)
    bound[section][key] = value
    with pytest.raises(ProvisioningInputError):
        verify_application(payload, bound, resolved)


def test_unresolved_render_blocked(payload):
    with pytest.raises(ApplicationRenderError, match="unresolved"):
        render_application(payload, chart_dir=CHART, helm_bin=HELM)


def test_only_used_dynamic_slots_are_required(payload):
    payload["env"] = payload["env"][:2]
    payload["required_env"] = ["APP_MODE", "API_KEY"]
    assert prepare_application(payload).bindings == []
    bound = resolve_application(payload, {})
    assert render_application(bound, chart_dir=CHART, helm_bin=HELM)


def test_helm_errors_never_escape(payload, resolved, monkeypatch, capsys):
    marker = b"RAW_ENV_AND_PASSWORD_MUST_NOT_ESCAPE"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, marker, marker)
    )
    with pytest.raises(ApplicationRenderError) as caught:
        render_application(resolve_application(payload, resolved), chart_dir=CHART)
    assert marker.decode() not in str(caught.value)
    output = capsys.readouterr()
    assert marker.decode() not in output.out + output.err


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unresolved", "raw", "invalid_port"])
def test_direct_helm_rejects_invalid_values(tmp_path, payload, resolved, mutation):
    bound = resolve_application(payload, resolved)
    if mutation == "duplicate":
        bound["env"].append(bound["env"][0])
    elif mutation == "missing":
        bound["env"].pop()
    elif mutation == "unresolved":
        bound["database"]["host"] = None
    elif mutation == "raw":
        bound["env"][0]["value"] = "not-allowed"
    else:
        bound["application"]["port"] = 70000
    path = tmp_path / "values.json"
    path.write_text(json.dumps(bound))
    result = subprocess.run(
        [
            HELM,
            "template",
            bound["release_name"],
            str(CHART),
            "-n",
            bound["namespace"],
            "-f",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert b"kind: Deployment" not in result.stdout


def test_schema_stays_aligned():
    from cmp_contracts.shared_deployment_v2 import ApplicationValuesV2

    v1 = ApplicationValues.model_json_schema()
    v2 = ApplicationValuesV2.model_json_schema()
    v1["$defs"]["Image"]["properties"]["registry_auth_ref"] = {
        "type": "null",
        "default": None,
    }
    v2["$defs"]["Image"]["properties"]["registry_auth_ref"] = {
        "type": "null",
        "default": None,
    }
    for name in v1["$defs"].keys() & v2["$defs"].keys():
        assert v1["$defs"][name] == v2["$defs"][name]
    expected = {
        "$defs": {**v1.pop("$defs"), **v2.pop("$defs")},
        "oneOf": [v1, v2],
    }
    assert json.loads((CHART / "values.schema.json").read_text()) == expected


def test_no_environment_variables(payload):
    payload["required_env"] = []
    payload["env"] = []
    bound = resolve_application(payload, {})
    docs = {
        d["kind"]: d
        for d in yaml.safe_load_all(render_application(bound, chart_dir=CHART, helm_bin=HELM))
        if d
    }
    assert docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"] == []


def test_invalid_render_payload_does_not_disclose_raw_config(payload):
    marker = "SECRET_RAW_ENV_CONTENT"
    payload["env"][0]["value"] = marker
    with pytest.raises(ProvisioningInputError) as caught:
        render_application(payload, chart_dir=CHART, helm_bin=HELM)
    assert marker not in str(caught.value)
