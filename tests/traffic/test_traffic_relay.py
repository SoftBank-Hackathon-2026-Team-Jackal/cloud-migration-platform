"""중계 서버 배포 자산 생성(#63) 시험. 키·주소 같은 값은 인자로 받고 파일에 비밀을 쓰지 않는다."""

from __future__ import annotations

import pytest
import yaml
from cmp_traffic.relay import render_bootstrap, render_wireguard


def test_bootstrap_points_both_clusters_at_the_given_endpoints() -> None:
    doc = yaml.safe_load(
        render_bootstrap(origin=("alb.example", 80), target=("10.8.0.2", 8080), health_path="/h")
    )
    clusters = {c["name"]: c for c in doc["static_resources"]["clusters"]}
    origin = clusters["cluster_origin"]["load_assignment"]["endpoints"][0]["lb_endpoints"][0]
    target = clusters["cluster_target"]["load_assignment"]["endpoints"][0]["lb_endpoints"][0]
    assert origin["endpoint"]["address"]["socket_address"] == {
        "address": "alb.example",
        "port_value": 80,
    }
    assert target["endpoint"]["address"]["socket_address"]["port_value"] == 8080
    check = clusters["cluster_target"]["health_checks"][0]["http_health_check"]["path"]
    assert check == "/h"


def test_bootstrap_admin_listens_on_loopback_only() -> None:
    doc = yaml.safe_load(render_bootstrap(origin=("a", 80), target=("b", 80)))
    assert doc["admin"]["address"]["socket_address"]["address"] == "127.0.0.1"


@pytest.mark.parametrize("bad", ["a b", "a;b", "a\nb", "http://a", ""])
def test_bootstrap_rejects_unsafe_hostnames(bad: str) -> None:
    with pytest.raises(ValueError):
        render_bootstrap(origin=(bad, 80), target=("b", 80))


def test_wireguard_config_fixes_mtu_and_uses_placeholders_for_secrets() -> None:
    conf = render_wireguard(
        address="10.8.0.1/24", peer_allowed_ips="10.8.0.2/32", endpoint="203.0.113.5:51820"
    )
    assert "MTU = 1420" in conf
    assert "PersistentKeepalive = 25" in conf
    assert "<RELAY_PRIVATE_KEY>" in conf and "<PEER_PUBLIC_KEY>" in conf
    assert "Endpoint = 203.0.113.5:51820" in conf


def test_wireguard_rejects_values_that_could_inject_config_lines() -> None:
    with pytest.raises(ValueError):
        render_wireguard(address="10.8.0.1/24\nPostUp = rm -rf /", peer_allowed_ips="10.8.0.2/32")
