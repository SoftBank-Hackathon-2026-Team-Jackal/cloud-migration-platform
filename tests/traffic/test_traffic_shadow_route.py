"""그림자 리허설 라우트(Provisioning 83, #362). 끄면 라우트가 예전과 바이트 단위로 같아야 한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cmp_traffic.envoy import (
    SHADOW_CLUSTER,
    EnvoyProxy,
    ShadowMirror,
    build_route_config,
    route_shadow,
)
from cmp_traffic.relay import add_shadow_cluster, render_bootstrap, to_dynamic_bootstrap

WEIGHTS = {"origin": 100, "target": 0}

# 83 이전 build_route_config의 출력(가중치 90/10, version 7, target_host, stream /api/stream).
FROZEN = (
    '{"version_info": "7", "resources": [{"@type": "type.googleapis.com/envoy.config.route.v3.'
    'RouteConfiguration", "name": "local_route", "virtual_hosts": [{"name": "app", "domains": ["*"], '
    '"routes": [{"match": {"prefix": "/admin"}, "direct_response": {"status": 403, "body": '
    '{"inline_string": "forbidden\\n"}}}, {"match": {"prefix": "/__cmp"}, "direct_response": '
    '{"status": 403, "body": {"inline_string": "forbidden\\n"}}}, {"match": {"prefix": "/api/stream"}, '
    '"route": {"weighted_clusters": {"clusters": [{"name": "cluster_origin", "weight": 90, '
    '"response_headers_to_add": [{"header": {"key": "x-tp-env", "value": "origin"}}]}, {"name": '
    '"cluster_target", "weight": 10, "response_headers_to_add": [{"header": {"key": "x-tp-env", '
    '"value": "target"}}], "host_rewrite_literal": "app.example"}]}, "timeout": "0s"}}, {"match": '
    '{"prefix": "/"}, "route": {"weighted_clusters": {"clusters": [{"name": "cluster_origin", '
    '"weight": 90, "response_headers_to_add": [{"header": {"key": "x-tp-env", "value": "origin"}}]}, '
    '{"name": "cluster_target", "weight": 10, "response_headers_to_add": [{"header": {"key": '
    '"x-tp-env", "value": "target"}}], "host_rewrite_literal": "app.example"}]}, "timeout": "5s", '
    '"retry_policy": {"retry_on": "connect-failure", "num_retries": 2, "per_try_timeout": "3s"}}}]}]}]}'
)


def routes(doc: dict) -> list[dict]:
    return doc["resources"][0]["virtual_hosts"][0]["routes"]


def test_without_shadow_the_route_config_is_byte_for_byte_the_same_as_before() -> None:
    args = ({"origin": 90, "target": 10}, 7, "app.example", ("/api/stream",))
    assert json.dumps(build_route_config(*args)) == FROZEN
    assert json.dumps(build_route_config(*args, shadow=None)) == FROZEN


def test_shadow_adds_get_only_mirror_routes_for_allowed_paths_just_before_the_catch_all() -> None:
    shadow = ShadowMirror(allow_paths=("/api/messages", "/api/rooms"), percent=50)
    plain = routes(build_route_config(WEIGHTS, 1, stream_prefixes=("/api/stream",)))
    mirrored = routes(
        build_route_config(WEIGHTS, 1, stream_prefixes=("/api/stream",), shadow=shadow)
    )

    # 관리·내부·실시간 경로는 그대로 앞에, 마지막 "/"도 그대로다.
    assert mirrored[:3] == plain[:3]
    assert mirrored[-1] == plain[-1]
    added = mirrored[3:-1]
    assert [r["match"]["prefix"] for r in added] == ["/api/messages", "/api/rooms"]
    for route in added:
        assert route["match"]["headers"] == [{"name": ":method", "string_match": {"exact": "GET"}}]
        # 사용자 응답은 "/"와 같은 가중치·제한시간·재시도로 나간다. 복사만 더한다.
        action = dict(route["route"])
        policies = action.pop("request_mirror_policies")
        assert action == plain[-1]["route"]
        assert policies == [
            {
                "cluster": SHADOW_CLUSTER,
                "runtime_fraction": {"default_value": {"numerator": 50, "denominator": "HUNDRED"}},
            }
        ]
    assert SHADOW_CLUSTER == "cluster_shadow"


@pytest.mark.parametrize(
    "paths",
    [("api/x",), ("/",), ("/a",), ("/admin/x",), ("/__cmp/fault",)],
)
def test_shadow_paths_must_be_absolute_and_never_overlap_blocked_paths(paths) -> None:
    with pytest.raises(ValueError):
        ShadowMirror(allow_paths=paths, percent=100)


def test_an_empty_allow_list_mirrors_nothing() -> None:
    with pytest.raises(ValueError):
        ShadowMirror(allow_paths=(), percent=100)


@pytest.mark.parametrize("percent", [0, -1, 101])
def test_the_mirror_percent_is_between_1_and_100(percent) -> None:
    with pytest.raises(ValueError):
        ShadowMirror(allow_paths=("/api",), percent=percent)


def test_the_shadow_in_a_route_file_is_read_back() -> None:
    shadow = ShadowMirror(allow_paths=("/api/messages",), percent=30)
    assert route_shadow(build_route_config(WEIGHTS, 1, shadow=shadow)) == shadow
    assert route_shadow(build_route_config(WEIGHTS, 1)) is None


class FakeAdmin:
    def __init__(self, rds: Path) -> None:
        self.rds = rds

    def __call__(self, url: str) -> dict:
        version = json.loads(self.rds.read_text())["version_info"] if self.rds.exists() else ""
        return {
            "stats": [
                {"name": "http.ingress.rds.local_route.version_text", "value": version},
                {"name": "http.ingress.rds.local_route.update_rejected", "value": 0},
            ]
        }


def make_proxy(tmp_path: Path) -> EnvoyProxy:
    rds = tmp_path / "rds.json"
    return EnvoyProxy(
        rds_path=rds,
        fetch=FakeAdmin(rds),
        confirm_timeout=0.05,
        poll_interval=0.01,
        stream_prefixes=("/api/stream",),
    )


def test_set_shadow_keeps_the_current_weights_and_off_restores_the_plain_routes(tmp_path) -> None:
    proxy = make_proxy(tmp_path)
    proxy.apply({"origin": 90, "target": 10})
    plain = routes(json.loads(proxy.rds_path.read_text()))

    shadow = ShadowMirror(allow_paths=("/api/messages",), percent=100)
    assert proxy.set_shadow(shadow) is True
    doc = json.loads(proxy.rds_path.read_text())
    assert route_shadow(doc) == shadow
    assert proxy.target_weight() == 10

    assert proxy.set_shadow(None) is True
    assert routes(json.loads(proxy.rds_path.read_text())) == plain


def test_turning_shadow_off_twice_does_not_rewrite_the_route_file(tmp_path) -> None:
    proxy = make_proxy(tmp_path)
    proxy.apply(WEIGHTS)
    before = proxy.rds_path.read_text()
    assert proxy.set_shadow(None) is False
    assert proxy.rds_path.read_text() == before


def test_set_shadow_without_a_readable_route_file_refuses(tmp_path) -> None:
    proxy = make_proxy(tmp_path)
    with pytest.raises(RuntimeError):
        proxy.set_shadow(ShadowMirror(allow_paths=("/api",), percent=100))
    assert not proxy.rds_path.exists()


def test_a_later_weight_change_drops_the_mirror_routes(tmp_path) -> None:
    """가중치 Activity는 그림자 설정을 모른다. 이어서 쓰면 복사가 꺼지는 쪽(안전한 쪽)으로 간다."""
    proxy = make_proxy(tmp_path)
    proxy.apply(WEIGHTS)
    proxy.set_shadow(ShadowMirror(allow_paths=("/api",), percent=100))
    make_proxy(tmp_path).apply({"origin": 90, "target": 10})
    assert route_shadow(json.loads(proxy.rds_path.read_text())) is None


def test_the_shadow_cluster_is_added_to_a_bootstrap_on_loopback_once() -> None:
    static = render_bootstrap(("origin.example", 80), ("10.200.0.2", 8080))
    dynamic, _ = to_dynamic_bootstrap(static, "/etc/envoy/cds/cds.json")
    for bootstrap in (static, dynamic):
        added = add_shadow_cluster(bootstrap)
        assert "cluster_shadow" in added
        assert "127.0.0.1" in added
        assert add_shadow_cluster(added) == added  # 두 번 실행해도 같다
    import yaml

    doc = yaml.safe_load(add_shadow_cluster(dynamic))
    names = [c["name"] for c in doc["static_resources"]["clusters"]]
    assert names == ["cluster_origin", "cluster_shadow"]
    shadow = doc["static_resources"]["clusters"][1]
    socket = shadow["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]
    assert socket["socket_address"] == {"address": "127.0.0.1", "port_value": 18090}
    assert "health_checks" not in shadow
    assert doc["dynamic_resources"]  # 동적 대상은 그대로
