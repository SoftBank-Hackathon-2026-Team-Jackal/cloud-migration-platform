"""그림자 리허설 Activity(Provisioning 83, #362): 켜기·요약·끄기(멱등, 수집기가 죽어도 Envoy 끄기)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cmp_traffic.envoy import EnvoyObserver, EnvoyProxy, build_route_config, route_shadow
from cmp_traffic.shadow import Collector, Reply, serve_control
from cmp_worker.activities import traffic as traffic_activities
from cmp_worker.activities import traffic_shadow
from cmp_worker.workers.relay import relay_activities
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

START = {
    "migration_id": "m1",
    "run_id": "r1",
    "allow_paths": ["/api/messages", "/api/stats"],
    "percent": 100,
    "rate_per_sec": 20,
}


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


@pytest.fixture
def relay(tmp_path, monkeypatch):
    rds = tmp_path / "rds.json"
    rds.write_text(json.dumps(build_route_config({"origin": 100, "target": 0}, 1)))

    def ports():
        proxy = EnvoyProxy(rds, fetch=FakeAdmin(rds), confirm_timeout=0.05, poll_interval=0.01)
        return proxy, EnvoyObserver(fetch=FakeAdmin(rds))

    monkeypatch.setattr(traffic_activities, "build_ports", ports)
    return rds


@pytest.fixture
def collector(monkeypatch):
    def fetch(address, path, headers):
        return Reply(200, b'{"ok": true}', 5.0)

    instance = Collector(
        addresses=lambda: (("origin.example", 80), ("10.200.0.4", 8080)), fetch=fetch, workers=0
    )
    server = serve_control(instance, "127.0.0.1", 0)
    monkeypatch.setenv("TRAFFIC_SHADOW_API", f"http://127.0.0.1:{server.server_address[1]}")
    yield instance
    server.shutdown()
    server.server_close()


@pytest.fixture
def no_collector(monkeypatch):
    monkeypatch.setenv("TRAFFIC_SHADOW_API", "http://127.0.0.1:1")  # 아무도 듣지 않는 포트


async def run(fn, payload):
    return await ActivityEnvironment().run(fn, payload)


async def test_start_registers_the_run_then_turns_on_the_envoy_copy(relay, collector) -> None:
    assert await run(traffic_shadow.shadow_start, START) == {"ok": True, "code": None}
    assert collector.active_run_id() == "r1"
    shadow = route_shadow(json.loads(relay.read_text()))
    assert shadow is not None
    assert shadow.allow_paths == ("/api/messages", "/api/stats") and shadow.percent == 100


async def test_summary_returns_the_collector_summary(relay, collector) -> None:
    await run(traffic_shadow.shadow_start, START)
    collector.mirror("GET", "/api/messages?after=1", {"Host": "a-shadow"})
    collector.drain()
    summary = await run(traffic_shadow.shadow_summary, {"run_id": "r1"})
    assert summary["run_id"] == "r1" and summary["compared"] == 1 and summary["match"] == 1
    assert set(summary) == {
        "run_id",
        "requests",
        "compared",
        "match",
        "mismatch",
        "skipped",
        "target_5xx",
        "origin_p99_ms",
        "target_p99_ms",
        "noise_fields",
        "samples",
    }


async def test_stop_turns_the_copy_off_and_is_idempotent(relay, collector) -> None:
    await run(traffic_shadow.shadow_start, START)
    assert await run(traffic_shadow.shadow_stop, {"run_id": "r1"}) == {"ok": True, "code": None}
    assert route_shadow(json.loads(relay.read_text())) is None
    assert collector.active_run_id() is None
    before = relay.read_text()
    assert await run(traffic_shadow.shadow_stop, {"run_id": "r1"}) == {"ok": True, "code": None}
    assert relay.read_text() == before  # 두 번째 끄기는 라우트 파일을 다시 쓰지 않는다


async def test_stop_still_turns_the_envoy_copy_off_when_the_collector_is_dead(
    relay, collector, monkeypatch
) -> None:
    await run(traffic_shadow.shadow_start, START)
    monkeypatch.setenv("TRAFFIC_SHADOW_API", "http://127.0.0.1:1")
    assert await run(traffic_shadow.shadow_stop, {"run_id": "r1"}) == {"ok": True, "code": None}
    assert route_shadow(json.loads(relay.read_text())) is None


async def test_start_without_a_collector_never_touches_envoy(relay, no_collector) -> None:
    before = relay.read_text()
    result = await run(traffic_shadow.shadow_start, START)
    assert result == {"ok": False, "code": "SHADOW_UNAVAILABLE"}
    assert relay.read_text() == before


@pytest.mark.parametrize("paths", [[], ["/"], ["/admin"], ["api"]])
async def test_start_with_an_unsafe_allow_list_is_refused(relay, collector, paths) -> None:
    before = relay.read_text()
    result = await run(traffic_shadow.shadow_start, {**START, "allow_paths": paths})
    assert result == {"ok": False, "code": "SHADOW_UNAVAILABLE"}
    assert relay.read_text() == before
    assert collector.active_run_id() is None


async def test_start_ends_the_collector_run_if_envoy_does_not_take_the_copy(
    tmp_path, collector, monkeypatch
) -> None:
    rds = tmp_path / "missing.json"  # 라우트 파일이 없어 가중치를 모른다

    def ports():
        proxy = EnvoyProxy(rds, fetch=FakeAdmin(rds), confirm_timeout=0.05, poll_interval=0.01)
        return proxy, EnvoyObserver(fetch=FakeAdmin(rds))

    monkeypatch.setattr(traffic_activities, "build_ports", ports)
    result = await run(traffic_shadow.shadow_start, START)
    assert result == {"ok": False, "code": "SHADOW_UNAVAILABLE"}
    assert collector.active_run_id() is None and not rds.exists()


async def test_start_on_a_relay_without_a_route_file_setting_is_unavailable(
    collector, monkeypatch
) -> None:
    monkeypatch.delenv("TRAFFIC_RDS_PATH", raising=False)
    result = await run(traffic_shadow.shadow_start, START)
    assert result == {"ok": False, "code": "SHADOW_UNAVAILABLE"}
    assert collector.active_run_id() is None


async def test_summary_without_a_collector_or_run_is_shadow_unavailable(
    relay, collector, monkeypatch
) -> None:
    with pytest.raises(Exception) as unknown:
        await run(traffic_shadow.shadow_summary, {"run_id": "nope"})
    monkeypatch.setenv("TRAFFIC_SHADOW_API", "http://127.0.0.1:1")
    with pytest.raises(Exception) as dead:
        await run(traffic_shadow.shadow_summary, {"run_id": "r1"})
    for info in (unknown, dead):
        error = info.value
        assert isinstance(error, ApplicationError) and error.type == "SHADOW_UNAVAILABLE"
        assert error.non_retryable


def test_the_relay_worker_serves_the_shadow_activities_next_to_the_traffic_ones() -> None:
    served = relay_activities()
    assert traffic_activities.traffic_set_weight in served
    for fn in (
        traffic_shadow.shadow_start,
        traffic_shadow.shadow_summary,
        traffic_shadow.shadow_stop,
    ):
        assert fn in served
