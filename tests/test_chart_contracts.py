import json
from pathlib import Path

import pytest
from cmp_contracts.chart import PostgresChartValues
from pydantic import ValidationError

FIXTURE = Path(__file__).parent / "fixtures/contracts/postgres-values.json"


def values():
    return json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("environment", ["SANDBOX", "PRODUCTION"])
@pytest.mark.parametrize("provider", ["AWS", "GCP", "ON_PREM"])
def test_same_values_contract_works_without_cloud_credentials(environment, provider):
    payload = values()
    payload.update(environment=environment, provider=provider)
    result = PostgresChartValues.model_validate(payload)
    assert result.resources.memory_mib == 512
    assert result.storage.size_gib == 20
    assert result.database.secret_name == "sandbox-db-auth"
    assert PostgresChartValues.model_validate_json(result.model_dump_json()) == result


def test_missing_storage_class_and_secret_reference_are_field_errors():
    payload = values()
    del payload["storage"]["class_name"]
    del payload["database"]["secret_name"]
    with pytest.raises(ValidationError) as caught:
        PostgresChartValues.model_validate(payload)
    paths = {item["loc"] for item in caught.value.errors()}
    assert ("storage", "class_name") in paths
    assert ("database", "secret_name") in paths


@pytest.mark.parametrize(
    "field,value",
    [("secret_name", "Invalid Secret"), ("version", "15"), ("password", "do-not-log")],
)
def test_database_contract_rejects_invalid_references_and_secret_plaintext(field, value):
    payload = values()
    payload["database"][field] = value
    with pytest.raises(ValidationError) as caught:
        PostgresChartValues.model_validate(payload)
    assert "do-not-log" not in str(caught.value)


def test_render_output_references_keep_chart_values_and_manifest_roles_distinct():
    from cmp_contracts.artifact import RenderedChart

    payload = {"contract_version": "1"}
    for field, kind in (("chart", "CHART"), ("values", "VALUES"), ("manifest", "MANIFEST")):
        payload[field] = {
            "id": field,
            "version": "1",
            "kind": kind,
            "sha256": "a" * 64,
            "size_bytes": 3,
        }
    output = RenderedChart.model_validate(payload)
    assert RenderedChart.model_validate_json(output.model_dump_json()) == output
    payload["manifest"]["kind"] = "VALUES"
    with pytest.raises(ValidationError):
        RenderedChart.model_validate(payload)
