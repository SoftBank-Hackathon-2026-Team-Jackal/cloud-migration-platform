"""Ticket 27: target DB logical-replication settings and the replication-capable check.

Seams: the shared chart Values contract, real `helm template` output, the pure probe judge, the
v2 shared renderer default, the ON_PREM Agent double and the Agent report → handoff refs.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from cmp_contracts.chart import PostgresChartValues
from cmp_providers.artifacts import canonical_json_bytes
from cmp_providers.db_replication import (
    DEFAULT_PARAMETERS,
    judge_probe,
    parameters_summary,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "infra/helm/postgres"
FIXTURE = ROOT / "tests/fixtures/contracts/postgres-values.json"
HELM = os.environ.get("HELM_BIN", "helm")
PARAMS = {
    "wal_level": "logical",
    "max_replication_slots": 10,
    "max_wal_senders": 10,
    "app": {"node_name": "on-prem-demo-db"},
}


def values(parameters=None):
    data = json.loads(FIXTURE.read_text())
    if parameters is not None:
        data["database"]["parameters"] = parameters
    return data


def template(tmp_path, data):
    path = tmp_path / "values.json"
    path.write_text(json.dumps(data))
    return subprocess.run(
        [HELM, "template", data["release_name"], str(CHART), "-n", data["namespace"], "-f", path],
        capture_output=True,
        text=True,
        check=False,
    )


def container(stdout):
    sts = next(d for d in yaml.safe_load_all(stdout) if d and d["kind"] == "StatefulSet")
    return sts["spec"]["template"]["spec"]["containers"][0]


# --- contract ------------------------------------------------------------------------------
def test_values_without_parameters_keep_their_bytes():
    raw = FIXTURE.read_text()
    model = PostgresChartValues.model_validate_json(raw)
    assert model.database.parameters is None
    assert model.model_dump(mode="json")["database"] == json.loads(raw)["database"]
    # Same canonical bytes (and digest) as a pre-ticket-27 model dump with no new key.
    dumped = canonical_json_bytes(model.model_dump(mode="json"))
    assert b"parameters" not in dumped
    assert dumped == canonical_json_bytes(
        PostgresChartValues.model_validate(json.loads(dumped)).model_dump(mode="json")
    )


def test_parameters_round_trip():
    model = PostgresChartValues.model_validate(values(PARAMS))
    assert model.model_dump(mode="json")["database"]["parameters"] == PARAMS


def test_empty_app_settings_are_not_serialised():
    params = {k: v for k, v in PARAMS.items() if k != "app"}
    model = PostgresChartValues.model_validate(values(params))
    assert "app" not in model.model_dump(mode="json")["database"]["parameters"]


@pytest.mark.parametrize(
    "bad",
    [
        PARAMS | {"shared_preload_libraries": "evil"},
        PARAMS | {"wal_level": "minimal"},
        PARAMS | {"max_wal_senders": -1},
        PARAMS | {"max_replication_slots": 65},
        PARAMS | {"max_wal_senders": "10"},
        PARAMS | {"app": {"Node-Name": "x"}},
        PARAMS | {"app": {"node_name": "has space"}},
        PARAMS | {"app": {"node_name": "x;drop"}},
        PARAMS | {"app": {f"k{i}": "v" for i in range(9)}},
    ],
)
def test_unlisted_or_invalid_parameters_are_rejected(bad):
    with pytest.raises(ValidationError):
        PostgresChartValues.model_validate(values(bad))


# --- chart ---------------------------------------------------------------------------------
def test_chart_passes_parameters_as_postgres_args(tmp_path):
    result = template(tmp_path, values(PARAMS))
    assert result.returncode == 0, result.stderr
    assert container(result.stdout)["args"] == [
        "-c",
        "wal_level=logical",
        "-c",
        "max_replication_slots=10",
        "-c",
        "max_wal_senders=10",
        "-c",
        "app.node_name=on-prem-demo-db",
    ]


def test_chart_without_parameters_renders_no_args(tmp_path):
    result = template(tmp_path, values())
    assert result.returncode == 0, result.stderr
    assert "args" not in container(result.stdout)


@pytest.mark.parametrize(
    "bad",
    [
        PARAMS | {"fsync": "off"},
        PARAMS | {"wal_level": "minimal"},
        PARAMS | {"app": {"node_name": "a b"}},
    ],
)
def test_schema_rejects_unknown_or_invalid_parameters(tmp_path, bad):
    result = template(tmp_path, values(bad))
    assert result.returncode != 0
    assert "schema" in result.stderr


# --- probe judge ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "probe,expected",
    [
        ("160004|true|logical|10|10", ("160004", True, None)),
        ("160004|true|replica|10|10", ("160004", False, "WAL_LEVEL_NOT_LOGICAL")),
        ("160004|false|logical|10|10", ("160004", False, "REPLICATION_ROLE_MISSING")),
        ("160004|true|logical|0|10", ("160004", False, "REPLICATION_SLOTS_UNAVAILABLE")),
        ("160004|true|logical|10|0", ("160004", False, "WAL_SENDERS_UNAVAILABLE")),
        (" 160004|true|logical|4|4\n", ("160004", True, None)),
    ],
)
def test_probe_requires_role_and_settings(probe, expected):
    assert judge_probe(probe) == expected


@pytest.mark.parametrize("probe", ["", "160004|true", "160004|true|logical|x|10"])
def test_unreadable_probe_is_not_capable(probe):
    _, capable, code = judge_probe(probe)
    assert (capable, code) == (False, "REPLICATION_PROBE_UNREADABLE")


def test_preview_summary_names_the_settings():
    model = PostgresChartValues.model_validate(values(PARAMS))
    assert parameters_summary(model) == (
        " · 논리 복제 설정 wal_level=logical · 슬롯 10 · 송신자 10"
    )
    assert parameters_summary(PostgresChartValues.model_validate(values())) == ""


# --- v2 renderer default ---------------------------------------------------------------------
def test_v2_renderer_defaults_to_logical_replication():
    from test_shared_deployment_v2 import render, request_payload

    rendered = render(request_payload())
    db = next(r for r in rendered.resources if r.id == "postgres-main")
    params = db.helm_values.database.parameters
    assert params.model_dump(mode="json") == DEFAULT_PARAMETERS | {
        "app": {"node_name": "on-prem-cmp-test-001-db"}
    }
    sts = next(d for d in yaml.safe_load_all(db.manifest_yaml) if d["kind"] == "StatefulSet")
    args = sts["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "wal_level=logical" in args and "app.node_name=on-prem-cmp-test-001-db" in args
