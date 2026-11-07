"""원본·대상 건강 지표(cmp_traffic.health) 단위 테스트. Envoy 관리 API는 가짜 값으로 대신한다."""

from __future__ import annotations

from cmp_traffic.health import EnvoyReader, Reading, parse_counters, report, window


def stats(origin: tuple[int, int, int], target: tuple[int, int, int]) -> dict:
    out = []
    for side, (total, five, timeout) in (("origin", origin), ("target", target)):
        out += [
            {"name": f"cluster.cluster_{side}.upstream_rq_total", "value": total},
            {"name": f"cluster.cluster_{side}.upstream_rq_5xx", "value": five},
            {"name": f"cluster.cluster_{side}.upstream_rq_timeout", "value": timeout},
        ]
    return {"stats": out}


def buckets(side: str, counts: dict[str, int]) -> str:
    name = "envoy_cluster_upstream_rq_time_bucket"
    return "\n".join(
        f'{name}{{envoy_cluster_name="cluster_{side}",le="{le}"}} {n}' for le, n in counts.items()
    )


def reading(at: float, origin, target, text: str = "") -> Reading:
    return Reading(at=at, counters=parse_counters(stats(origin, target)), buckets_text=text)


def test_a_window_is_the_difference_between_two_readings() -> None:
    before = reading(0, (100, 0, 0), (50, 1, 0))
    after = reading(5, (200, 0, 0), (150, 21, 4))
    target = window(before, after, "target")
    assert target["requests"] == 100
    assert target["errors_5xx"] == 20 and target["timeouts"] == 4
    assert target["failed"] == 24
    assert target["error_rate"] == 0.24
    assert target["rps"] == 20.0
    assert window(before, after, "origin")["error_rate"] == 0.0


def test_no_requests_means_unknown_rate_and_p99_not_zero() -> None:
    same = reading(0, (10, 0, 0), (10, 0, 0))
    later = reading(5, (10, 0, 0), (10, 0, 0))
    side = window(same, later, "target")
    assert side["requests"] == 0
    assert side["error_rate"] is None and side["p99_ms"] is None


def test_p99_comes_from_the_histogram_buckets_of_the_window() -> None:
    first = buckets("target", {"50": 0, "500": 0, "+Inf": 0})
    last = buckets("target", {"50": 90, "500": 100, "+Inf": 100})
    before = reading(0, (0, 0, 0), (0, 0, 0), first)
    after = reading(5, (0, 0, 0), (100, 0, 0), last)
    assert window(before, after, "target")["p99_ms"] == 500.0


def test_an_envoy_restart_does_not_produce_negative_counts() -> None:
    before = reading(0, (1000, 50, 0), (1000, 50, 0))
    after = reading(5, (30, 1, 0), (30, 1, 0))
    origin = window(before, after, "origin")
    assert origin["requests"] == 30 and origin["failed"] == 1


def test_report_has_both_sides_and_the_window_length() -> None:
    body = report(reading(10, (0, 0, 0), (0, 0, 0)), reading(15, (5, 0, 0), (5, 5, 0)))
    assert body["window_seconds"] == 5
    assert body["target"]["error_rate"] == 1.0
    assert set(body) == {"window_seconds", "origin", "target"}


def test_reader_asks_envoy_only_for_the_cluster_counters_and_buckets() -> None:
    urls: list[str] = []

    def fetch(url: str) -> dict:
        urls.append(url)
        return stats((1, 0, 0), (2, 0, 0))

    def fetch_text(url: str) -> str:
        urls.append(url)
        return ""

    got = EnvoyReader("http://envoy:9901/", fetch, fetch_text, clock=lambda: 7.0).read()
    assert got.at == 7.0 and got.counters["target"]["total"] == 2
    assert urls[0].startswith("http://envoy:9901/stats?format=json&filter=")
    assert urls[1].startswith("http://envoy:9901/stats/prometheus?filter=")
