"""`/system/fault`: Tools 화면이 정한 장애 명령을 중계 서버 리포터가 보고 응답으로 받아 가고 실제 상태를 알린다."""

from __future__ import annotations

import pytest
from cmp_api.fault_control import OBSERVED_STALE_SECONDS, FaultControl
from cmp_api.main import app
from cmp_api.routes.pulse import (
    get_fault_control,
    get_health_store,
    get_report_token,
    get_tools_enabled,
)
from cmp_api.traffic_health import HealthStore
from fastapi.testclient import TestClient


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def window() -> dict:
    side = {
        "requests": 10,
        "errors_5xx": 0,
        "timeouts": 0,
        "failed": 0,
        "error_rate": 0.0,
        "rps": 2.0,
        "p99_ms": 25.0,
    }
    return {"window_seconds": 5, "origin": side, "target": side}


@pytest.fixture
def setup():
    clock = Clock()
    control = FaultControl(clock=clock)
    state = {"tools": True}
    app.dependency_overrides[get_fault_control] = lambda: control
    app.dependency_overrides[get_health_store] = lambda: HealthStore(clock=clock)
    app.dependency_overrides[get_report_token] = lambda: "secret-token"
    app.dependency_overrides[get_tools_enabled] = lambda: state["tools"]
    try:
        yield TestClient(app), clock, state
    finally:
        for dep in (get_fault_control, get_health_store, get_report_token, get_tools_enabled):
            app.dependency_overrides.pop(dep, None)


def report(http: TestClient, fault: dict | None = None):
    body = window() if fault is None else {**window(), "fault": fault}
    return http.post(
        "/system/traffic-health/report", json=body, headers={"X-Pulse-Token": "secret-token"}
    )


def test_the_switch_is_closed_unless_tools_are_enabled(setup) -> None:
    http, _, state = setup
    state["tools"] = False
    assert http.get("/system/fault").status_code == 404
    assert http.post("/system/fault", json={}).status_code == 404
    assert http.delete("/system/fault").status_code == 404
    # 보고는 그대로 받고, 명령은 내주지 않는다
    answer = report(http, {"active": False})
    assert answer.status_code == 200 and answer.json() == {"fault": None}


def test_turning_on_hands_the_command_to_the_relay_and_waits_for_the_target(setup) -> None:
    http, _, _ = setup
    view = http.post("/system/fault", json={"error_rate": 0.5, "delay_ms": 800}).json()
    command = view["desired"]
    assert command["action"] == "on" and command["duration_s"] == 300
    assert view["applied"] is None  # 아직 리포터의 보고가 없다

    assert report(http).json()["fault"] == command
    # 다음 보고에서 대상 앱이 실제로 켜졌다고 알린다
    report(
        http,
        {
            "active": True,
            "error_rate": 0.5,
            "delay_ms": 800,
            "remaining_s": 295,
            "applied_id": command["id"],
            "error": None,
        },
    )
    view = http.get("/system/fault").json()
    assert view["applied"] is True and view["observed"]["active"] is True


def test_turning_off_is_a_new_command(setup) -> None:
    http, _, _ = setup
    on = http.post("/system/fault", json={"error_rate": 1.0, "delay_ms": 0}).json()["desired"]
    off = http.delete("/system/fault").json()["desired"]
    assert off["action"] == "off" and off["id"] != on["id"]
    assert report(http).json()["fault"]["id"] == off["id"]


def test_an_expired_on_command_is_not_handed_out(setup) -> None:
    http, clock, _ = setup
    http.post("/system/fault", json={"error_rate": 0.5, "duration_s": 60})
    clock.now += 61
    assert report(http).json()["fault"] is None


def test_an_old_observation_is_not_shown_as_current(setup) -> None:
    http, clock, _ = setup
    report(http, {"active": True, "error_rate": 0.5})
    clock.now += OBSERVED_STALE_SECONDS + 1
    view = http.get("/system/fault").json()
    assert view["observed"] is None and view["observed_age_seconds"] > OBSERVED_STALE_SECONDS


@pytest.mark.parametrize(
    "body",
    [
        {"error_rate": 1.5},
        {"delay_ms": 20_000},
        {"duration_s": 0},
        {"error_rate": 0, "delay_ms": 0},
        {"error_rate": 0.5, "token": "x"},
    ],
)
def test_bad_settings_are_refused(setup, body) -> None:
    http, _, _ = setup
    assert http.post("/system/fault", json=body).status_code == 422


def test_the_report_still_needs_the_token(setup) -> None:
    http, _, _ = setup
    response = http.post("/system/traffic-health/report", json=window())
    assert response.status_code == 403


def fast_sync(http: TestClient, fault: dict | None = None, token: str = "secret-token"):
    body = {} if fault is None else {"fault": fault}
    return http.post("/system/traffic-health/fault", json=body, headers={"X-Pulse-Token": token})


def test_fast_sync_hands_out_the_command_and_records_the_observed_state(setup) -> None:
    http, _, state = setup
    command = http.post(
        "/system/fault", json={"error_rate": 0.5, "delay_ms": 800, "duration_s": 60}
    ).json()["desired"]
    answer = fast_sync(http, {"active": False})
    assert answer.status_code == 200 and answer.json()["fault"]["id"] == command["id"]
    fast_sync(
        http, {"active": True, "error_rate": 0.5, "delay_ms": 800, "applied_id": command["id"]}
    )
    view = http.get("/system/fault").json()
    assert view["applied"] is True and view["observed"]["active"] is True
    # 토큰이 틀리면 거절하고, Tools가 꺼져 있으면 명령을 내주지 않는다
    assert fast_sync(http, token="wrong").status_code == 403
    state["tools"] = False
    assert fast_sync(http, {"active": False}).json() == {"fault": None}
