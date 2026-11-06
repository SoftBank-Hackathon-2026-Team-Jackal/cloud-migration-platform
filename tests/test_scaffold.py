from pathlib import Path

import pytest
import yaml
from cmp_api.main import app
from cmp_contracts.migration import MigrationRequest, StepResult
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_health_is_local_and_does_not_require_temporal() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_contract_rejects_unplanned_target() -> None:
    # Ticket 44: the web targets (AWS, GCP, ON_PREM) are accepted; anything else and unknown
    # fields are refused instead of being dropped.
    request = MigrationRequest(project_id="demo")
    assert request.target_provider is None and request.target is None
    assert StepResult(step="analysis", detail="example").mode == "scaffold"
    assert MigrationRequest(project_id="demo", target_provider="ON_PREM").target_provider
    with pytest.raises(ValidationError):
        MigrationRequest(project_id="demo", target_provider="AZURE")
    with pytest.raises(ValidationError):
        MigrationRequest(project_id="demo", target={"provider": "GCP", "profile": "x"}, extra=1)


def test_checked_in_openapi_matches_api() -> None:
    path = Path(__file__).resolve().parents[1] / "docs/contracts/openapi.yaml"
    documented = yaml.safe_load(path.read_text(encoding="utf-8"))
    generated = app.openapi()
    assert documented["paths"] == generated["paths"]
    assert documented["components"] == generated["components"]
    assert {key: documented["info"][key] for key in generated["info"]} == generated["info"]
    assert documented["x-sdd-agreed-capabilities"]
    assert documented["x-sdd-unresolved-http-interface"]
