"""Ticket 19 (#79, D-16): single-variable DB connection URL delivered via a platform Secret."""

import json
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
import yaml
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.app_renderer import render_application
from cmp_providers.application import prepare_application, resolve_application
from cmp_providers.database_url import (
    URL_SECRET_KEY,
    connection_url,
    connection_url_secret_name,
)
from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2
from test_shared_deployment_v2 import APP_CHART, DB_CHART, request_payload

ROOT = Path(__file__).resolve().parents[1]
HELM = os.environ.get("HELM_BIN", "helm")


# --- shared builder (U3) -------------------------------------------------------------------


def test_url_has_the_agreed_shape():
    url = connection_url(
        username="appuser", password="s3cret", host="db.ns.svc", port=5432, database="appdb"
    )
    assert url == "postgresql://appuser:s3cret@db.ns.svc:5432/appdb"


def test_reserved_characters_are_percent_encoded_and_round_trip():
    password = "p@ss:w/rd?#%&= 한"
    url = connection_url(
        username="app user", password=password, host="db.ns.svc", port=5432, database="my/db"
    )
    parts = urlsplit(url)
    assert parts.scheme == "postgresql" and parts.hostname == "db.ns.svc" and parts.port == 5432
    assert unquote(parts.password) == password
    assert unquote(parts.username) == "app user"
    assert unquote(parts.path[1:]) == "my/db"
    assert "?" not in url.split("@", 1)[1] and "#" not in url


@pytest.mark.parametrize(
    "change",
    [{"host": "db.ns.svc/evil"}, {"host": ""}, {"port": 0}, {"username": ""}, {"password": ""}],
)
def test_invalid_parts_are_rejected(change):
    parts = {
        "username": "u",
        "password": "p",
        "host": "db.ns.svc",
        "port": 5432,
        "database": "d",
    } | change
    with pytest.raises(ValueError):
        connection_url(**parts)


def test_secret_name_convention():
    assert connection_url_secret_name("cmp-test-001-db") == "cmp-test-001-db-url"
    assert URL_SECRET_KEY == "url"
    assert len(connection_url_secret_name("a" * 53)) <= 63


# --- v2 contract + shared renderer (Sandbox and ON_PREM) ----------------------------------


def url_request(**db_changes):
    payload = request_payload()
    app = payload["resources"][1]
    app["env_bindings"] = {
        "DATABASE_URL": {"resource_id": "postgres-main", "output": "connection_url_secret_ref"},
        "API_KEY": {"secret_ref": {"name": "app-env", "key": "API_KEY"}},
    }
    payload["resources"][0]["database"].update(db_changes)
    return payload


def render(payload):
    return render_shared_deployment_v2(
        payload, database_chart_dir=DB_CHART, application_chart_dir=APP_CHART
    )


def test_v2_binding_renders_a_secret_reference_to_the_url_secret():
    result = render(url_request())
    db, app = result.resources
    assert db.outputs.connection_url_secret_ref.model_dump() == {
        "name": "cmp-test-001-db-url",
        "key": "url",
    }
    [env] = [item for item in app.helm_values.env if item.name == "DATABASE_URL"]
    assert env.source.model_dump() == {
        "kind": "SECRET",
        "name": "cmp-test-001-db-url",
        "key": "url",
    }
    deployment = next(
        d for d in yaml.safe_load_all(app.manifest_yaml) if d and d["kind"] == "Deployment"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    rendered = {item["name"]: item for item in container["env"]}
    assert rendered["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "cmp-test-001-db-url",
        "key": "url",
    }
    # Only the reference crosses the boundary; no URL or password text in Values or manifests.
    text = app.manifest_yaml + json.dumps(app.helm_values.model_dump(mode="json"))
    assert "postgresql://" not in text


def test_existing_field_outputs_are_unchanged():
    result = render(request_payload())
    app = result.resources[1]
    assert {item.name for item in app.helm_values.env} == {
        "DB_HOST",
        "DB_PORT",
        "DB_PASSWORD",
        "API_KEY",
    }


def test_url_secret_name_colliding_with_the_password_secret_is_rejected():
    payload = url_request(password_secret_ref={"name": "cmp-test-001-db-url", "key": "url"})
    with pytest.raises(ProvisioningInputError) as caught:
        render(payload)
    assert [issue.code for issue in caught.value.issues] == ["SECRET_NAME_CONFLICT"]


def test_url_secret_name_colliding_with_a_user_secret_is_rejected():
    payload = url_request()
    payload["resources"][1]["env_bindings"]["API_KEY"] = {
        "secret_ref": {"name": "cmp-test-001-db-url", "key": "API_KEY"}
    }
    with pytest.raises(ProvisioningInputError) as caught:
        render(payload)
    assert [issue.code for issue in caught.value.issues] == ["SECRET_NAME_CONFLICT"]


def test_unknown_output_is_rejected():
    payload = url_request()
    payload["resources"][1]["env_bindings"]["DATABASE_URL"]["output"] = "connection_url"
    with pytest.raises(ProvisioningInputError):
        render(payload)


def test_planning_requirement_accepts_the_output():
    from cmp_contracts.provisioning_v2 import ResourceOutputBinding

    binding = ResourceOutputBinding.model_validate(
        {"resource_id": "postgres-main", "output": "connection_url_secret_ref"}
    )
    assert binding.output == "connection_url_secret_ref"


# --- v1 Values (AWS/GCP production deploy, tickets 09/11) ---------------------------------


@pytest.fixture
def v1_payload():
    payload = json.loads((ROOT / "tests/fixtures/contracts/app-values.json").read_text())
    payload["required_env"] = ["APP_MODE", "DATABASE_URL"]
    payload["env"] = [item for item in payload["env"] if item["name"] == "APP_MODE"] + [
        {
            "name": "DATABASE_URL",
            "source": {"kind": "DATABASE", "field": "connection_url_secret_ref"},
        }
    ]
    return payload


def test_v1_plan_exposes_only_the_url_secret_slot(v1_payload):
    plan = prepare_application(v1_payload)
    assert [(r.target, r.source) for r in plan.bindings] == [
        ("database.url_secret_name", "DATABASE_URL_SECRET_NAME")
    ]
    # Unset optional slot keeps already-approved Values bytes unchanged.
    assert "url_secret_name" not in plan.values.model_dump(mode="json")["database"]


def test_v1_resolved_values_inject_the_url_secret(v1_payload):
    bound = resolve_application(v1_payload, {"DATABASE_URL_SECRET_NAME": "demo-db-url"})
    assert bound["database"]["url_secret_name"] == "demo-db-url"
    output = render_application(bound, chart_dir=ROOT / "infra/helm/application", helm_bin=HELM)
    deployment = next(d for d in yaml.safe_load_all(output) if d and d["kind"] == "Deployment")
    env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {"name": "demo-db-url", "key": "url"}


def test_v1_url_slot_must_be_unresolved_in_the_template(v1_payload):
    tampered = deepcopy(v1_payload)
    tampered["database"]["url_secret_name"] = "attacker-chosen"
    with pytest.raises(ProvisioningInputError):
        prepare_application(tampered)


def test_v1_existing_fixture_plan_is_unchanged():
    payload = json.loads((ROOT / "tests/fixtures/contracts/app-values.json").read_text())
    plan = prepare_application(payload)
    assert [rule.target for rule in plan.bindings] == ["database.host", "database.secret_name"]
