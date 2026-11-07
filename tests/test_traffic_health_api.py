"""`/system/traffic-health`: 중계 서버가 보낸 원본·대상 지표를 받고 요약해 내준다."""

from __future__ import annotations

import pytest
from cmp_api.main import app
from cmp_api.routes.pulse import get_health_store, get_report_token
from cmp_api.traffic_health import STALE_SECONDS, HealthStore
from fastapi.testclient import TestClient


def side(requests: int, failed: int, p99: float | None) -> dict:
    return {
        "requests": requests,
        "errors_5xx": failed,
        "timeouts": 0,
        "failed": failed,
        "error_rate": failed / requests if requests else None,
        "rps": requests / 5,
        "p99_ms": p99,
    }


def body(target_failed: int = 0, p99: float = 40.0) -> dict:
    return {
        "window_seconds": 5,
        "origin": side(100, 0, 30.0),
        "target": side(100, target_failed, p99),
    }


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def client():
    clock = Clock()
    store = HealthStore(clock=clock)
    app.dependency_overrides[get_health_store] = lambda: store
    app.dependency_overrides[get_report_token] = lambda: "secret-token"
    try:
        yield TestClient(app), clock
    finally:
        app.dependency_overrides.pop(get_health_store, None)
        app.dependency_overrides.pop(get_report_token, None)


def post(client: TestClient, payload: dict, token: str | None = "secret-token"):
    headers = {"X-Pulse-Token": token} if token else {}
    return client.post("/system/traffic-health/report", json=payload, headers=headers)


def test_report_needs_the_shared_token(client) -> None:
    http, _ = client
    assert post(http, body(), token=None).status_code == 403
    assert post(http, body(), token="wrong").status_code == 403
    assert post(http, body()).status_code == 204


def test_report_is_refused_while_the_token_is_not_configured() -> None:
    app.dependency_overrides[get_report_token] = lambda: ""
    try:
        assert post(TestClient(app), body()).status_code == 503
    finally:
        app.dependency_overrides.pop(get_report_token, None)


def test_summary_weights_the_error_rate_by_requests_and_keeps_the_worst_p99(client) -> None:
    http, _ = client
    post(http, body(target_failed=0, p99=40.0))
    post(http, body(target_failed=50, p99=900.0))
    got = http.get("/system/traffic-health").json()
    assert got["fresh"] is True
    assert got["summary"]["target"]["error_rate"] == 0.25
    assert got["summary"]["target"]["p99_ms"] == 900.0
    assert got["summary"]["origin"]["error_rate"] == 0.0
    assert len(got["series"]) == 2


def test_stale_reports_show_unknown_not_old_numbers(client) -> None:
    http, clock = client
    post(http, body(target_failed=10))
    clock.now += STALE_SECONDS + 1
    got = http.get("/system/traffic-health").json()
    assert got["fresh"] is False
    assert got["summary"]["target"]["error_rate"] is None
    assert len(got["series"]) == 1  # 지난 값은 그래프에는 남는다


def test_a_report_with_unknown_fields_is_rejected(client) -> None:
    http, _ = client
    assert post(http, {**body(), "host": "10.0.0.1"}).status_code == 422
