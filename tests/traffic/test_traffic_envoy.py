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
from temporalio.exceptions import ApplicationError


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
    first = int(json.loads(rds.read_text())["version_info"])
    proxy.apply({"origin": 50, "target": 50})
    assert int(json.loads(rds.read_text())["version_info"]) > first
    assert not list(tmp_path.glob("*.tmp"))


def test_a_new_proxy_instance_never_reuses_the_version_envoy_already_reported(tmp_path) -> None:
    """Activity마다 EnvoyProxy를 새로 만든다. 번호가 1부터 다시 시작하면 확인이 무의미해진다."""
    rds = tmp_path / "rds.json"
    admin = FakeAdmin(rds)
    seen: list[str] = []
    for weights in ({"origin": 100, "target": 0}, {"origin": 90, "target": 10}):
        proxy = EnvoyProxy(rds_path=rds, fetch=admin, confirm_timeout=0.05, poll_interval=0.01)
        proxy.apply(weights)
        seen.append(json.loads(rds.read_text())["version_info"])
    assert seen[0] != seen[1]


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


def test_route_retries_connect_failures_only() -> None:
    doc = build_route_config({"origin": 90, "target": 10}, version=1)
    route = doc["resources"][0]["virtual_hosts"][0]["routes"][-1]["route"]
    assert route["retry_policy"]["retry_on"] == "connect-failure"
    assert route["retry_policy"]["num_retries"] == 2


PROM_HEAD = "# TYPE envoy_cluster_upstream_rq_time histogram\n"


def prom(cluster: str, buckets: dict[str, int]) -> str:
    lines = [PROM_HEAD]
    for le, count in buckets.items():
        lines.append(
            f'envoy_cluster_upstream_rq_time_bucket{{envoy_cluster_name="cluster_{cluster}",le="{le}"}} {count}\n'
        )
    return "".join(lines)


def test_window_p99_comes_from_bucket_deltas_not_cumulative_history() -> None:
    from cmp_traffic.envoy import window_p99

    before = prom("target", {"10": 100, "100": 100, "1000": 100, "+Inf": 100})
    after = prom("target", {"10": 190, "100": 190, "1000": 200, "+Inf": 200})
    assert (
        window_p99(before, after, "target") == 1000
    )  # 새 100건 중 10건이 1초대 -> p99는 1000ms 버킷
    after_fast = prom("target", {"10": 200, "100": 200, "1000": 200, "+Inf": 200})
    assert window_p99(before, after_fast, "target") == 10


def test_window_p99_is_zero_without_new_samples() -> None:
    from cmp_traffic.envoy import window_p99

    same = prom("target", {"10": 5, "+Inf": 5})
    assert window_p99(same, same, "target") == 0


def test_observer_uses_prometheus_buckets_for_p99_when_available() -> None:
    counters = iter(
        [stats_payload(100, 0, 0, 0.0, "target"), stats_payload(160, 0, 0, 0.0, "target")]
    )
    texts = iter(
        [
            prom("target", {"10": 100, "1000": 100, "+Inf": 100}),
            prom("target", {"10": 150, "1000": 160, "+Inf": 160}),
        ]
    )
    observer = EnvoyObserver(
        fetch=lambda url: next(counters), fetch_text=lambda url: next(texts), sleep=lambda s: None
    )
    sample = observer.sample_target(5)
    assert sample.requests == 60 and sample.p99_ms == 1000


# ---- 실시간 연결 경로(#294): 제한시간·재시도 없이 ----


def _routes(doc: dict) -> list[dict]:
    return doc["resources"][0]["virtual_hosts"][0]["routes"]


def test_stream_routes_sit_before_the_catch_all_without_timeout_or_retry() -> None:
    doc = build_route_config(
        {"origin": 70, "target": 30},
        version=3,
        target_host="app.example",
        stream_prefixes=("/api/stream", "/events"),
    )
    routes = _routes(doc)
    prefixes = [r["match"]["prefix"] for r in routes]
    assert prefixes == ["/admin", "/__cmp", "/api/stream", "/events", "/"]
    catch_all = routes[-1]["route"]
    for stream in routes[2:4]:
        route = stream["route"]
        assert route["timeout"] == "0s"
        assert "retry_policy" not in route
        # 같은 가중치·같은 host_rewrite: 실시간 연결도 전환 비율을 그대로 따른다
        assert route["weighted_clusters"] == catch_all["weighted_clusters"]
    assert catch_all["timeout"] == "5s" and "retry_policy" in catch_all


def test_without_stream_prefixes_the_route_config_is_unchanged() -> None:
    clusters = [
        {
            "name": "cluster_origin",
            "weight": 90,
            "response_headers_to_add": [{"header": {"key": "x-tp-env", "value": "origin"}}],
        },
        {
            "name": "cluster_target",
            "weight": 10,
            "response_headers_to_add": [{"header": {"key": "x-tp-env", "value": "target"}}],
            "host_rewrite_literal": "app.example",
        },
    ]
    forbidden = {"status": 403, "body": {"inline_string": "forbidden\n"}}
    expected = [
        {"match": {"prefix": "/admin"}, "direct_response": forbidden},
        {"match": {"prefix": "/__cmp"}, "direct_response": forbidden},
        {
            "match": {"prefix": "/"},
            "route": {
                "weighted_clusters": {"clusters": clusters},
                "timeout": "5s",
                "retry_policy": {
                    "retry_on": "connect-failure",
                    "num_retries": 2,
                    "per_try_timeout": "3s",
                },
            },
        },
    ]
    doc = build_route_config({"origin": 90, "target": 10}, 4, target_host="app.example")
    assert _routes(doc) == expected
    assert (
        build_route_config(
            {"origin": 90, "target": 10}, 4, target_host="app.example", stream_prefixes=()
        )
        == doc
    )


@pytest.mark.parametrize(
    "bad", ["api/stream", "", "/", "/a", "/admin", "/admin/stream", "/__cmp", "/__cmpx"]
)
def test_stream_prefixes_must_be_paths_that_do_not_touch_blocked_paths(bad: str) -> None:
    with pytest.raises(ValueError):
        build_route_config({"origin": 100, "target": 0}, 1, stream_prefixes=(bad,))


def test_apply_keeps_the_stream_routes_every_time_the_weights_change(tmp_path) -> None:
    rds = tmp_path / "rds.json"
    proxy = EnvoyProxy(
        rds_path=rds,
        fetch=FakeAdmin(rds),
        confirm_timeout=0.05,
        poll_interval=0.01,
        stream_prefixes=("/api/stream",),
    )
    for target in (10, 50):
        proxy.apply({"origin": 100 - target, "target": target})
        routes = _routes(json.loads(rds.read_text()))
        stream = routes[2]
        assert stream["match"]["prefix"] == "/api/stream"
        assert stream["route"]["timeout"] == "0s"
        weights = {c["name"]: c["weight"] for c in stream["route"]["weighted_clusters"]["clusters"]}
        assert weights == {"cluster_origin": 100 - target, "cluster_target": target}
    assert proxy.target_weight() == 50


def test_relay_worker_reads_stream_prefixes_from_the_environment(tmp_path, monkeypatch) -> None:
    from cmp_worker.activities import traffic as activities

    monkeypatch.setenv("TRAFFIC_RDS_PATH", str(tmp_path / "rds.json"))
    monkeypatch.setenv("TRAFFIC_STREAM_PREFIXES", " /api/stream, ,/events ")
    proxy, _ = activities.build_ports()
    assert proxy.stream_prefixes == ("/api/stream", "/events")

    monkeypatch.delenv("TRAFFIC_STREAM_PREFIXES")
    proxy, _ = activities.build_ports()
    assert proxy.stream_prefixes == ()

    monkeypatch.setenv("TRAFFIC_STREAM_PREFIXES", "/admin")
    with pytest.raises(ApplicationError) as error:
        activities.build_ports()
    assert error.value.type == "RelayNotConfigured" and error.value.non_retryable
