"""Envoy 어댑터(#62) 단위 테스트. Envoy 관리 API는 가짜 fetch로 대신한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cmp_traffic import Sample
from cmp_traffic.envoy import (
    EnvoyObserver,
    EnvoyProxy,
    build_route_config,
    parse_cluster_stats,
)


def test_route_config_splits_weights_between_the_two_clusters() -> None:
    doc = build_route_config({"origin": 90, "target": 10}, version=7)
    assert doc["version_info"] == "7"
    route = doc["resources"][0]["virtual_hosts"][0]["routes"][-1]["route"]
    clusters = {c["name"]: c["weight"] for c in route["weighted_clusters"]["clusters"]}
    assert clusters == {"cluster_origin": 90, "cluster_target": 10}


def test_route_config_blocks_internal_paths_before_anything_else() -> None:
    routes = build_route_config({"origin": 100, "target": 0}, version=1)["resources"][0][
        "virtual_hosts"
    ][0]["routes"]
    assert routes[0]["direct_response"]["status"] == 403
    assert routes[0]["match"]["prefix"] == "/admin"


def test_target_host_rewrite_only_applies_to_the_target_cluster() -> None:
    doc = build_route_config({"origin": 50, "target": 50}, version=1, target_host="app.example")
    clusters = doc["resources"][0]["virtual_hosts"][0]["routes"][-1]["route"]["weighted_clusters"][
        "clusters"
    ]
    by_name = {c["name"]: c for c in clusters}
    assert by_name["cluster_target"]["host_rewrite_literal"] == "app.example"
    assert "host_rewrite_literal" not in by_name["cluster_origin"]


@pytest.mark.parametrize("weights", [{"origin": 60, "target": 60}, {"origin": -1, "target": 101}])
def test_route_config_rejects_weights_that_do_not_sum_to_100(weights) -> None:
    with pytest.raises(ValueError):
        build_route_config(weights, version=1)


class FakeAdmin:
    """Envoy 관리 API 흉내: 파일이 바뀌면 version_text를 따라 올린다."""

    def __init__(self, rds: Path, mode: str = "accept") -> None:
        self.rds, self.mode, self.rejected, self.calls = rds, mode, 0, 0

    def __call__(self, url: str) -> dict:
        self.calls += 1
        if self.mode == "reject" and self.calls > 1:  # 첫 호출은 쓰기 전 기준값 읽기
            self.rejected = 1
        version = json.loads(self.rds.read_text())["version_info"] if self.rds.exists() else ""
        if self.mode in ("stale", "reject"):  # 거부된 설정은 version_text가 올라가지 않는다
            version = "0"
        return {
            "stats": [
                {"name": "http.ingress.rds.local_route.version_text", "value": version},
                {"name": "http.ingress.rds.local_route.update_rejected", "value": self.rejected},
            ]
        }


def make_proxy(tmp_path: Path, mode: str = "accept") -> tuple[EnvoyProxy, Path]:
    rds = tmp_path / "rds.json"
    proxy = EnvoyProxy(
        rds_path=rds, fetch=FakeAdmin(rds, mode), confirm_timeout=0.05, poll_interval=0.01
    )
    return proxy, rds


def test_apply_writes_the_config_and_returns_once_envoy_reports_that_version(tmp_path) -> None:
    proxy, rds = make_proxy(tmp_path)
    proxy.apply({"origin": 90, "target": 10})
    assert json.loads(rds.read_text())["version_info"] == "1"
    proxy.apply({"origin": 50, "target": 50})
    assert json.loads(rds.read_text())["version_info"] == "2"
    assert not list(tmp_path.glob("*.tmp"))


def test_apply_raises_when_envoy_rejects_the_config(tmp_path) -> None:
    proxy, _ = make_proxy(tmp_path, "reject")
    with pytest.raises(RuntimeError, match="rejected"):
        proxy.apply({"origin": 90, "target": 10})


def test_apply_raises_when_envoy_never_loads_the_version(tmp_path) -> None:
    proxy, _ = make_proxy(tmp_path, "stale")
    with pytest.raises(RuntimeError, match="timeout"):
        proxy.apply({"origin": 90, "target": 10})


def stats_payload(total: int, rq_5xx: int, timeout: int, p99: float, cluster: str) -> dict:
    prefix = f"cluster.cluster_{cluster}."
    return {
        "stats": [
            {"name": prefix + "upstream_rq_total", "value": total},
            {"name": prefix + "upstream_rq_5xx", "value": rq_5xx},
            {"name": prefix + "upstream_rq_timeout", "value": timeout},
            {
                "histograms": {
                    "supported_quantiles": [50, 90, 99],
                    "computed_quantiles": [
                        {
                            "name": prefix + "upstream_rq_time",
                            "values": [{"interval": 1}, {"interval": 2}, {"interval": p99}],
                        }
                    ],
                }
            },
        ]
    }


def test_parse_cluster_stats_reads_counters_and_p99() -> None:
    parsed = parse_cluster_stats(stats_payload(10, 1, 2, 120.0, "target"), "target")
    assert parsed == {"requests": 10, "failed": 3, "p99_ms": 120.0}


def test_observer_returns_the_delta_over_the_observe_window() -> None:
    calls = iter(
        [stats_payload(100, 2, 0, 80.0, "target"), stats_payload(160, 3, 1, 90.0, "target")]
    )
    slept: list[float] = []
    observer = EnvoyObserver(fetch=lambda url: next(calls), sleep=slept.append)
    assert observer.sample_target(5) == Sample(requests=60, failed=2, p99_ms=90.0)
    assert slept == [5]


def test_observer_treats_missing_p99_as_zero_and_never_goes_negative() -> None:
    first = stats_payload(100, 0, 0, float("nan"), "origin")
    second = stats_payload(90, 0, 0, float("nan"), "origin")  # 카운터가 줄었다면(재시작) 0으로
    calls = iter([first, second])
    observer = EnvoyObserver(fetch=lambda url: next(calls), sleep=lambda s: None)
    assert observer.sample_origin(1) == Sample(requests=0, failed=0, p99_ms=0)
