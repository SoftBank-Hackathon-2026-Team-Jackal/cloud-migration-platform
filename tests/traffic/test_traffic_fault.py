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
