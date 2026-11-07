"""그림자 수집기(Provisioning 83, #362): 원본 2번·대상 1번 비교, 잡음 학습, 마스킹, 상한, 멱등 중지."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from cmp_traffic.shadow import (
    MAX_SAMPLES,
    Collector,
    Reply,
    RunSpec,
    ShadowRun,
    TokenBucket,
    flatten,
    mask,
    serve_control,
    strip_shadow_host,
)

ORIGIN = ("origin.example", 80)
TARGET = ("10.200.0.4", 8080)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeUpstream:
    """(호스트, 경로)마다 정해 둔 응답을 차례로 돌려준다. 받은 요청을 기록한다."""

    def __init__(self, replies: dict[tuple[str, str], list[Reply]]) -> None:
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, address: tuple[str, int], path: str, headers: dict) -> Reply:
        self.calls.append((address[0], path, headers))
        queue = self.replies[(address[0], path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def body(doc) -> bytes:
    return json.dumps(doc).encode()


def make_run(upstream, **spec) -> ShadowRun:
    values = {"run_id": "r1", "allow_paths": ("/api",), "rate_per_sec": 100, **spec}
    return ShadowRun(
        RunSpec(**values),
        origin=ORIGIN,
        target=TARGET,
        fetch=upstream,
        clock=Clock(),
    )


def test_the_shadow_host_suffix_envoy_appends_is_removed() -> None:
    assert strip_shadow_host("app.example-shadow") == "app.example"
    assert strip_shadow_host("app.example-shadow:8080") == "app.example:8080"
    assert strip_shadow_host("app.example") == "app.example"


def test_json_is_flattened_to_pointers_with_sorted_keys() -> None:
    assert flatten({"b": [1, {"c/d": None}], "a~": "x", "e": {}}) == {
        "/a~0": "x",
        "/b/0": 1,
        "/b/1/c~1d": None,
        "/e": {},
    }


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        ("secret-token", "<string:12>"),
        (42, "<number>"),
        (True, "<bool>"),
        (None, "null"),
        ([1, 2], "<array:2>"),
        ({"k": 1}, "<object:1>"),
    ],
)
def test_values_are_masked_to_their_type_and_size(value, masked) -> None:
    assert mask(value) == masked


def test_a_same_response_matches_and_the_request_is_sent_twice_to_origin_and_once_to_target() -> (
    None
):
    doc = {"items": [1, 2], "ok": True}
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/m?after=1"): [Reply(200, body(doc), 10.0)],
            ("10.200.0.4", "/api/m?after=1"): [
                Reply(200, body({"ok": True, "items": [1, 2]}), 30.0)
            ],
        }
    )
    run = make_run(upstream)
    run.handle("GET", "/api/m?after=1", {"Host": "app.example-shadow", "Accept": "*/*"})

    hosts = [c[0] for c in upstream.calls]
    assert hosts.count("origin.example") == 2 and hosts.count("10.200.0.4") == 1
    assert all(c[2]["Host"] == "app.example" for c in upstream.calls)
    summary = run.summary()
    assert (summary["requests"], summary["compared"], summary["match"]) == (1, 1, 1)
    assert summary["mismatch"] == 0 and summary["samples"] == []
    assert summary["origin_p99_ms"] == 10.0 and summary["target_p99_ms"] == 30.0


def test_fields_that_differ_between_the_two_origin_calls_are_learned_as_noise() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/x"): [
                Reply(200, body({"now": "t1", "n": 1}), 5.0),
                Reply(200, body({"now": "t2", "n": 1}), 5.0),
            ],
            ("10.200.0.4", "/api/x"): [Reply(200, body({"now": "t3", "n": 1}), 5.0)],
        }
    )
    run = make_run(upstream)
    run.handle("GET", "/api/x", {"Host": "a-shadow"})
    summary = run.summary()
    assert summary["noise_fields"] == ["/now"]
    assert summary["match"] == 1 and summary["mismatch"] == 0


def test_noise_learned_later_also_clears_an_earlier_mismatch() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/x"): [
                Reply(200, body({"now": "t1"}), 5.0),
                Reply(200, body({"now": "t1"}), 5.0),
                Reply(200, body({"now": "t2"}), 5.0),
                Reply(200, body({"now": "t3"}), 5.0),
            ],
            ("10.200.0.4", "/api/x"): [Reply(200, body({"now": "t9"}), 5.0)],
        }
    )
    run = make_run(upstream)
    run.handle("GET", "/api/x", {})
    assert run.summary()["mismatch"] == 1
    run.handle("GET", "/api/x", {})
    summary = run.summary()
    assert summary["mismatch"] == 0 and summary["match"] == 2 and summary["samples"] == []


def test_a_real_difference_is_a_mismatch_with_masked_values_and_no_query_string() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/x?token=abc"): [Reply(200, body({"name": "alice"}), 5.0)],
            ("10.200.0.4", "/api/x?token=abc"): [Reply(500, body({"name": "bob!"}), 5.0)],
        }
    )
    run = make_run(upstream)
    run.handle("GET", "/api/x?token=abc", {})
    summary = run.summary()
    assert summary["mismatch"] == 1 and summary["target_5xx"] == 1
    assert summary["samples"] == [
        {
            "method": "GET",
            "path": "/api/x",
            "status_origin": 200,
            "status_target": 500,
            "diff": [{"pointer": "/name", "origin": "<string:5>", "target": "<string:4>"}],
        }
    ]
    assert "alice" not in json.dumps(summary) and "token" not in json.dumps(summary)


def test_non_json_bodies_are_compared_by_hash_only() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/p"): [Reply(200, b"<html>a</html>", 5.0)],
            ("10.200.0.4", "/api/p"): [Reply(200, b"<html>b</html>", 5.0)],
        }
    )
    run = make_run(upstream)
    run.handle("GET", "/api/p", {})
    sample = run.summary()["samples"][0]
    assert sample["diff"][0]["pointer"] == ""
    assert "html" not in json.dumps(run.summary())


def test_at_most_20_mismatch_samples_are_kept() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/x"): [Reply(200, body({"v": 1}), 5.0)],
            ("10.200.0.4", "/api/x"): [Reply(200, body({"v": 2}), 5.0)],
        }
    )
    run = make_run(upstream)
    for _ in range(30):
        run.handle("GET", "/api/x", {})
    summary = run.summary()
    assert summary["mismatch"] == 30
    assert len(summary["samples"]) == MAX_SAMPLES == 20


def test_origin_re_requests_are_capped_per_second() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/x"): [Reply(200, b"{}", 5.0)],
            ("10.200.0.4", "/api/x"): [Reply(200, b"{}", 5.0)],
        }
    )
    run = make_run(upstream, rate_per_sec=4)  # 원본 재요청 초당 4번 = 비교 2건
    for _ in range(5):
        run.handle("GET", "/api/x", {})
    summary = run.summary()
    assert (summary["requests"], summary["compared"], summary["skipped"]) == (5, 2, 3)
    assert [c[0] for c in upstream.calls].count("origin.example") == 4


def test_token_bucket_refills_with_time() -> None:
    clock = Clock()
    bucket = TokenBucket(2, clock)
    assert bucket.take(2) and not bucket.take(2)
    clock.now += 1
    assert bucket.take(2)


@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/api/x"), ("GET", "/other"), ("DELETE", "/api/x")]
)
def test_writes_and_paths_outside_the_allow_list_are_never_re_sent(method, path) -> None:
    upstream = FakeUpstream({})
    run = make_run(upstream)
    run.handle(method, path, {})
    assert upstream.calls == []
    assert run.summary()["skipped"] == 1


def test_an_unreachable_origin_is_skipped_and_an_unreachable_target_counts_as_5xx() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/x"): [Reply(None, b"", 5.0)],
            ("10.200.0.4", "/api/x"): [Reply(200, b"{}", 5.0)],
        }
    )
    run = make_run(upstream)
    run.handle("GET", "/api/x", {})
    assert run.summary()["skipped"] == 1
    upstream.replies[("origin.example", "/api/x")] = [Reply(200, b"{}", 5.0)]
    upstream.replies[("10.200.0.4", "/api/x")] = [Reply(None, b"", 5.0)]
    run.handle("GET", "/api/x", {})
    summary = run.summary()
    assert summary["target_5xx"] == 1 and summary["mismatch"] == 1
    assert summary["samples"][0]["status_target"] == 0


def make_collector(upstream) -> Collector:
    return Collector(addresses=lambda: (ORIGIN, TARGET), fetch=upstream, workers=0)


def test_stop_is_idempotent_and_a_stopped_run_ignores_new_copies() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/x"): [Reply(200, b"{}", 5.0)],
            ("10.200.0.4", "/api/x"): [Reply(200, b"{}", 5.0)],
        }
    )
    collector = make_collector(upstream)
    collector.start(RunSpec(run_id="r1", allow_paths=("/api",), rate_per_sec=20))
    collector.mirror("GET", "/api/x", {})
    collector.drain()
    assert collector.stop("r1") is True
    assert collector.stop("r1") is False
    assert collector.stop("unknown") is False
    collector.mirror("GET", "/api/x", {})
    collector.drain()
    assert collector.summary("r1")["requests"] == 1


def test_a_new_run_ends_the_previous_one() -> None:
    collector = make_collector(FakeUpstream({}))
    collector.start(RunSpec(run_id="r1", allow_paths=("/api",)))
    collector.start(RunSpec(run_id="r2", allow_paths=("/api",)))
    assert collector.active_run_id() == "r2"
    assert collector.summary("r1") is not None
    assert collector.summary("nope") is None


def test_the_control_api_starts_summarizes_and_stops_runs() -> None:
    upstream = FakeUpstream(
        {
            ("origin.example", "/api/x"): [Reply(200, b"{}", 5.0)],
            ("10.200.0.4", "/api/x"): [Reply(200, b"{}", 5.0)],
        }
    )
    collector = make_collector(upstream)
    server = serve_control(collector, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:

        def call(method: str, path: str, doc=None):
            data = json.dumps(doc).encode() if doc is not None else None
            request = urllib.request.Request(base + path, data=data, method=method)
            request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read() or b"{}")

        spec = {"run_id": "r1", "migration_id": "m1", "allow_paths": ["/api"], "percent": 100}
        assert call("POST", "/runs", {**spec, "rate_per_sec": 20})[0] == 200
        assert call("POST", "/runs", {"run_id": "bad", "allow_paths": []})[0] == 400
        collector.mirror("GET", "/api/x", {})
        collector.drain()
        status, summary = call("GET", "/runs/r1/summary")
        assert status == 200 and summary["run_id"] == "r1" and summary["match"] == 1
        assert call("GET", "/runs/nope/summary")[0] == 404
        assert call("DELETE", "/runs/r1") == (200, {"run_id": "r1", "stopped": True})
        assert call("DELETE", "/runs/r1") == (200, {"run_id": "r1", "stopped": False})
    finally:
        server.shutdown()
        server.server_close()


def test_the_listeners_only_bind_to_loopback() -> None:
    collector = make_collector(FakeUpstream({}))
    with pytest.raises(ValueError):
        serve_control(collector, "0.0.0.0", 0)
