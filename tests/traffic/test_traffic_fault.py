"""데모 장애 스위치 조작(cmp_traffic.fault): 대상 주소를 Envoy에서 읽고 토큰을 붙여 보낸다."""

from __future__ import annotations

import json

from cmp_traffic.fault import main, target_url

DUMP = {
    "configs": [
        {
            "dynamic_active_clusters": [
                {
                    "cluster": {
                        "name": "cluster_target",
                        "load_assignment": {
                            "endpoints": [
                                {
                                    "lb_endpoints": [
                                        {
                                            "endpoint": {
                                                "address": {
                                                    "socket_address": {
                                                        "address": "10.200.0.4",
                                                        "port_value": 8080,
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                }
                            ]
                        },
                    }
                }
            ]
        }
    ]
}


class Recorder:
    def __init__(self, status: int = 200) -> None:
        self.calls: list = []
        self.status = status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        return self.status, {"active": method == "POST"}


def test_the_target_comes_from_envoy_unless_given() -> None:
    assert target_url({}, fetch=lambda url: DUMP) == "http://10.200.0.4:8080"
    given = {"FAULT_TARGET_URL": "http://127.0.0.1:18003/"}
    assert target_url(given, fetch=lambda url: {}) == "http://127.0.0.1:18003"


def test_on_posts_the_settings_with_the_token(capsys) -> None:
    send = Recorder()
    code = main(
        ["on", "--error-rate", "0.3", "--delay-ms", "500", "--duration", "60"],
        env={"FAULT_TOKEN": "t0k"},
        send=send,
        fetch=lambda url: DUMP,
    )
    assert code == 0
    method, url, headers, body = send.calls[0]
    assert (method, url) == ("POST", "http://10.200.0.4:8080/__cmp/fault")
    assert headers["X-Fault-Token"] == "t0k"
    assert body == {"error_rate": 0.3, "delay_ms": 500, "duration_s": 60}
    assert "t0k" not in capsys.readouterr().out  # 토큰은 출력하지 않는다


def test_off_and_status_use_delete_and_get() -> None:
    send = Recorder()
    env = {"FAULT_TOKEN": "t", "FAULT_TARGET_URL": "http://app:8080"}
    assert main(["off"], env=env, send=send) == 0
    assert main(["status"], env=env, send=send) == 0
    assert [(m, u) for m, u, _, _ in send.calls] == [
        ("DELETE", "http://app:8080/__cmp/fault"),
        ("GET", "http://app:8080/__cmp/fault"),
    ]


def test_without_a_token_nothing_is_sent() -> None:
    send = Recorder()
    assert main(["on"], env={}, send=send, fetch=lambda url: DUMP) == 2
    assert send.calls == []


def test_a_refused_switch_is_a_failure_exit() -> None:
    env = {"FAULT_TOKEN": "t", "FAULT_TARGET_URL": "http://app:8080"}
    assert main(["on"], env=env, send=Recorder(status=403)) == 1


class Target:
    """대상 앱 `/__cmp/fault`를 흉내 낸다: 받은 요청을 기록하고 상태를 바꾼다."""

    def __init__(self, status: int = 200) -> None:
        self.calls: list = []
        self.status = status
        self.on: dict | None = None

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers.get("X-Fault-Token")))
        if self.status != 200:
            return self.status, {}
        if method == "POST":
            data = json.loads(body)
            self.on = {**data, "until": "2026-11-07T10:05:00Z"}
        elif method == "DELETE":
            self.on = None
        return 200, {"active": self.on is not None, "fault": self.on or {}}


def make_sync(target: Target):
    from cmp_traffic.health import FaultSync

    return FaultSync("tok", {"FAULT_TARGET_URL": "http://app:8080"}, send=target)


def test_sync_applies_a_new_command_once() -> None:
    target = Target()
    sync = make_sync(target)
    command = {"id": "a1", "action": "on", "error_rate": 0.5, "delay_ms": 800, "duration_s": 300}
    sync.apply(command)
    sync.apply(command)
    posts = [c for c in target.calls if c[0] == "POST"]
    assert posts == [("POST", "http://app:8080/__cmp/fault", "tok")]
    assert sync.applied_id == "a1"
    sync.apply({"id": "b2", "action": "off"})
    assert target.calls[-1][0] == "DELETE" and sync.applied_id == "b2"


def test_sync_ignores_no_command_and_retries_after_a_failure() -> None:
    target = Target(status=503)
    sync = make_sync(target)
    sync.apply(None)
    assert target.calls == []
    sync.apply({"id": "a1", "action": "off"})
    assert sync.applied_id is None and sync.error == "status 503"
    target.status = 200
    sync.apply({"id": "a1", "action": "off"})
    assert sync.applied_id == "a1" and sync.error is None


def test_sync_reports_what_the_target_actually_has() -> None:
    from datetime import datetime

    target = Target()
    sync = make_sync(target)
    assert sync.state()["active"] is False
    sync.apply({"id": "a1", "action": "on", "error_rate": 0.5, "delay_ms": 800, "duration_s": 300})
    now = datetime.fromisoformat("2026-11-07T10:00:00+00:00").timestamp()
    state = sync.state(clock=lambda: now)
    assert state == {
        "applied_id": "a1",
        "error": None,
        "active": True,
        "error_rate": 0.5,
        "delay_ms": 800,
        "remaining_s": 300,
    }


def test_sync_says_unknown_when_the_target_cannot_be_read() -> None:
    sync = make_sync(Target(status=403))
    assert sync.state()["active"] is None


def test_fast_sync_applies_and_reports_the_new_state_at_once() -> None:
    # 5초 지표 보고를 기다리지 않는다: 명령을 받아 적용하면 바뀐 실제 상태를 같은 차례에 다시 보낸다.
    target = Target()
    sync = make_sync(target)
    command = {"id": "c3", "action": "on", "error_rate": 0.5, "delay_ms": 800, "duration_s": 300}
    sent: list[dict] = []

    def post_fn(body: dict) -> tuple[int, dict]:
        sent.append(body)
        return 200, {"fault": command}

    assert sync.sync_once(post_fn) == 200
    assert [b["fault"]["active"] for b in sent] == [False, True]
    assert sent[1]["fault"]["applied_id"] == "c3"
    # 같은 명령이면 다시 적용하지 않고 한 번만 보낸다
    sent.clear()
    sync.sync_once(post_fn)
    assert len(sent) == 1
    assert [c[0] for c in target.calls].count("POST") == 1


def test_fast_sync_url_sits_next_to_the_report_url() -> None:
    from cmp_traffic.health import fault_sync_url

    assert (
        fault_sync_url("https://h.example/system/traffic-health/report")
        == "https://h.example/system/traffic-health/fault"
    )
    assert fault_sync_url("https://h.example/other") == ""
    assert fault_sync_url("") == ""
