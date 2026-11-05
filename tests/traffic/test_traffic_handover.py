"""Provisioning 결과(ProvisioningResult)를 트래픽 인계값(Handover)으로 바꾸는 규칙(#128)."""

from __future__ import annotations

import pytest
from cmp_traffic import check_handover
from cmp_traffic.handover import handover_from_result


def result(status="READY", **application) -> dict:
    app = {
        "ready": True,
        "external_host": "10.200.0.2",
        "external_port": 8080,
        "external_endpoint": "http://10.200.0.2:8080",
        "port": 8080,
        "health_path": "/healthz",
        "scheme": "http",
        "tls": False,
    }
    app.update(application)
    return {"status": status, "application": app}


def test_a_ready_result_becomes_a_handover_the_relay_can_use() -> None:
    handover = handover_from_result(result())
    assert handover.status == "READY" and handover.ready
    assert handover.external_endpoint == "http://10.200.0.2:8080"
    assert (handover.port, handover.health_path) == (8080, "/healthz")
    assert check_handover(handover).ok


def test_host_and_port_win_over_the_joined_endpoint_and_port_falls_back_to_the_app_port() -> None:
    handover = handover_from_result(
        result(external_endpoint="http://stale:1", external_port=None, port=3000)
    )
    assert handover.external_endpoint == "http://10.200.0.2:3000" and handover.port == 3000


@pytest.mark.parametrize(
    ("status", "expected"),
    [("FAILED", "FAILED"), ("UNKNOWN", "UNKNOWN"), ("INFRASTRUCTURE_READY", "UNKNOWN")],
)
def test_anything_but_ready_never_passes_the_precheck(status, expected) -> None:
    handover = handover_from_result(result(status=status))
    assert handover.status == expected
    assert not check_handover(handover).ok


def test_an_application_that_is_not_ready_or_missing_blocks_the_shift() -> None:
    assert not check_handover(handover_from_result(result(ready=False))).ok
    no_app = handover_from_result({"status": "READY", "application": None})
    assert not no_app.ready and not check_handover(no_app).ok
    assert not check_handover(handover_from_result({"status": "READY"})).ok


def test_https_or_tls_is_not_supported_in_p0_so_it_blocks() -> None:
    assert not handover_from_result(result(tls=True)).ready


def test_credentials_in_the_endpoint_are_rejected() -> None:
    with pytest.raises(ValueError):
        handover_from_result(
            result(external_host=None, external_port=None, external_endpoint="http://u:p@h:80")
        )


def test_a_missing_health_path_uses_the_default() -> None:
    assert handover_from_result(result(health_path=None)).health_path == "/health"


def test_field_names_match_the_real_provisioning_contract() -> None:
    from cmp_contracts.provisioning_result import ApplicationHandoff

    application = ApplicationHandoff(
        external_host="10.200.0.2",
        external_port=8080,
        port=8080,
        health_path="/healthz",
        ready=True,
    ).model_dump()
    handover = handover_from_result({"status": "READY", "application": application})
    assert check_handover(handover).ok
    assert handover.external_endpoint == "http://10.200.0.2:8080"
