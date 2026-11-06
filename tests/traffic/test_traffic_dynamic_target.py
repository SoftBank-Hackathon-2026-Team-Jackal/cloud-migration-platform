"""대상 주소를 파일(CDS)로 바꾸는 중계 서버: 설정 생성, Envoy 어댑터, `traffic_prepare`의 안전 조건.

Envoy 관리 API는 가짜(FakeEnvoy)로 대신한다. 가짜는 두 파일(rds.json, cds.json)을 읽어 진짜 Envoy처럼 version_text와
`/config_dump`(활성 클러스터)를 보여 준다.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest
import yaml
from cmp_traffic.envoy import EnvoyObserver, EnvoyProxy, build_route_config, cluster_target_address
from cmp_traffic.relay import render_bootstrap, render_target_cds
from cmp_worker.activities import traffic as activities
from cmp_worker.activities.traffic import target_allowed, traffic_prepare
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

ORIGINAL = ("10.200.0.3", 8080)  # what the relay was set up with (the OCI stand-in)
HANDOVER = {
    "status": "READY",
    "ready": True,
    "external_endpoint": "http://10.200.0.2:8080",
    "health_path": "/healthz",
    "port": 8080,
}


# ---- 설정 생성 ----


def test_a_dynamic_bootstrap_has_no_static_target_and_watches_the_cluster_file() -> None:
    doc = yaml.safe_load(
        render_bootstrap(
            origin=("alb.example", 80),
            target=ORIGINAL,
            cds_path="/etc/envoy/cds/cds.json",
        )
    )
    names = [c["name"] for c in doc["static_resources"]["clusters"]]
    assert names == ["cluster_origin"]  # the target comes from the file, never from here
    source = doc["dynamic_resources"]["cds_config"]["path_config_source"]
    assert source == {
        "path": "/etc/envoy/cds/cds.json",
        "watched_directory": {"path": "/etc/envoy/cds"},
    }


def test_without_a_cluster_path_the_bootstrap_is_the_static_one_as_before() -> None:
    doc = yaml.safe_load(render_bootstrap(origin=("alb.example", 80), target=ORIGINAL))
    assert [c["name"] for c in doc["static_resources"]["clusters"]] == [
        "cluster_origin",
        "cluster_target",
    ]
    assert "dynamic_resources" not in doc


def test_the_initial_cluster_file_carries_the_target_and_a_version() -> None:
    doc = json.loads(render_target_cds(ORIGINAL, "/h", version=7))
    assert doc["version_info"] == "7"
    (cluster,) = doc["resources"]
    assert cluster["@type"].endswith("envoy.config.cluster.v3.Cluster")
    assert cluster["name"] == "cluster_target"
    socket = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]
    assert socket["socket_address"] == {"address": "10.200.0.3", "port_value": 8080}
    assert cluster["health_checks"][0]["http_health_check"]["path"] == "/h"


@pytest.mark.parametrize("bad", ["a b", "a;b", "http://a", ""])
def test_the_initial_cluster_file_rejects_unsafe_hosts(bad: str) -> None:
    with pytest.raises(ValueError):
        render_target_cds((bad, 80))


# ---- Envoy 가짜 ----


class FakeEnvoy:
    def __init__(self, rds: Path, cds: Path, *, reject_cds=False, never_active=False) -> None:
        self.rds, self.cds = rds, cds
        self.reject_cds, self.never_active = reject_cds, never_active
        self.first_address = self._cds_address()
        self.cds_calls = 0

    def _version(self, path: Path) -> str:
        return json.loads(path.read_text())["version_info"] if path.exists() else ""

    def _cds_address(self) -> tuple[str, int] | None:
        if not self.cds.exists():
            return None
        cluster = json.loads(self.cds.read_text())["resources"][0]
        socket = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"][
            "address"
        ]["socket_address"]
        return socket["address"], socket["port_value"]

    def __call__(self, url: str) -> dict:
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/config_dump":
            address = self.first_address if self.never_active else self._cds_address()
            cluster = {"name": "cluster_target"}
            if address:
                cluster["load_assignment"] = {
                    "endpoints": [
                        {
                            "lb_endpoints": [
                                {
                                    "endpoint": {
                                        "address": {
                                            "socket_address": {
                                                "address": address[0],
                                                "port_value": address[1],
                                            }
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                }
            return {"configs": [{"dynamic_active_clusters": [{"cluster": cluster}]}]}
        pattern = urllib.parse.parse_qs(parsed.query).get("filter", [""])[0]
        if "cds" in pattern:
            self.cds_calls += 1
            rejected = 1 if self.reject_cds and self.cds_calls > 1 else 0
            version = "0" if self.reject_cds else self._version(self.cds)
            return {
                "stats": [
                    {"name": "cluster_manager.cds.version_text", "value": version},
                    {"name": "cluster_manager.cds.update_rejected", "value": rejected},
                ]
            }
        if "rds" in pattern:
            return {
                "stats": [
                    {
                        "name": "http.ingress.rds.local_route.version_text",
                        "value": self._version(self.rds),
                    },
                    {"name": "http.ingress.rds.local_route.update_rejected", "value": 0},
                ]
            }
        return {"stats": []}  # observer counters: no traffic


@pytest.fixture
def relay(tmp_path, monkeypatch):
    rds, cds = tmp_path / "rds.json", tmp_path / "cds.json"
    rds.write_text(json.dumps(build_route_config({"origin": 100, "target": 0}, 1)))
    cds.write_text(render_target_cds(ORIGINAL, "/healthz", version=1))
    state: dict = {}

    def build(**options) -> FakeEnvoy:
        envoy = FakeEnvoy(rds, cds, **options)
        proxy = EnvoyProxy(
            rds,
            fetch=envoy,
            cds_path=options.pop("cds_path", cds),
            confirm_timeout=0.05,
            poll_interval=0.01,
        )
        observer = EnvoyObserver(fetch=envoy, sleep=lambda seconds: None)
        monkeypatch.setattr(activities, "build_ports", lambda: (proxy, observer))
        state.update(envoy=envoy, proxy=proxy)
        return envoy

    monkeypatch.setattr(activities, "health_status", lambda host, port, path: 200)
    monkeypatch.setattr(activities, "RETARGET_CONFIRM_S", 0.05)
    monkeypatch.setattr(activities, "RETARGET_POLL_S", 0.01)
    monkeypatch.delenv("TRAFFIC_TARGET_ALLOWED_CIDRS", raising=False)
    build.rds, build.cds, build.state = rds, cds, state
    return build


async def prepare(handover: dict | None = None) -> dict:
    payload = {
        "handover": handover or HANDOVER,
        "policy": {"observe_seconds": 0.01, "min_requests": 1},
    }
    return await ActivityEnvironment().run(traffic_prepare, payload)


def address_in(path: Path) -> tuple[str, int]:
    cluster = json.loads(path.read_text())["resources"][0]
    socket = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]
    return socket["socket_address"]["address"], socket["socket_address"]["port_value"]


async def refused(handover: dict | None = None) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        await prepare(handover)
    return caught.value


# ---- Envoy 어댑터 ----


def test_set_target_replaces_the_cluster_file_and_returns_once_envoy_reports_the_version(
    relay,
) -> None:
    relay()
    proxy = relay.state["proxy"]
    first = int(json.loads(relay.cds.read_text())["version_info"])
    proxy.set_target("10.200.0.2", 8080, "/healthz")
    assert address_in(relay.cds) == ("10.200.0.2", 8080)
    assert int(json.loads(relay.cds.read_text())["version_info"]) > first
    assert not list(relay.cds.parent.glob("*.tmp"))


def test_set_target_without_a_cluster_path_is_an_error(tmp_path) -> None:
    proxy = EnvoyProxy(tmp_path / "rds.json")
    with pytest.raises(RuntimeError, match="not configured"):
        proxy.set_target("10.200.0.2", 8080)


def test_set_target_raises_when_envoy_rejects_the_cluster(relay) -> None:
    relay(reject_cds=True)
    with pytest.raises(RuntimeError, match="rejected"):
        relay.state["proxy"].set_target("10.200.0.2", 8080)


def test_set_target_refuses_an_unsafe_host_before_writing(relay) -> None:
    relay()
    with pytest.raises(ValueError):
        relay.state["proxy"].set_target("10.0.0.1;rm", 8080)
    assert address_in(relay.cds) == ORIGINAL


def test_the_target_weight_is_read_from_the_route_file(relay) -> None:
    relay()
    proxy = relay.state["proxy"]
    assert proxy.target_weight() == 0
    proxy.apply({"origin": 90, "target": 10})
    assert proxy.target_weight() == 10
    relay.rds.write_text("not json")
    assert proxy.target_weight() is None


def test_the_target_address_is_read_from_active_dynamic_clusters_too() -> None:
    dump = {
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
                                                            "address": "10.200.0.2",
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
    assert cluster_target_address(dump) == ("10.200.0.2", 8080)


# ---- traffic_prepare ----


async def test_a_target_that_already_matches_is_left_alone(relay) -> None:
    relay()
    before = relay.cds.read_text()
    same = {**HANDOVER, "external_endpoint": "http://10.200.0.3:8080"}
    result = await prepare(same)
    assert result["checks"][0]["state"] == "done"
    assert relay.cds.read_text() == before  # nothing was rewritten


async def test_a_different_target_inside_the_tunnel_range_is_set_and_confirmed(relay) -> None:
    relay()
    result = await prepare()
    assert address_in(relay.cds) == ("10.200.0.2", 8080)
    assert [check["id"] for check in result["checks"]] == ["external_endpoint", "health"]
    # the shift then starts from origin 100%
    assert relay.state["proxy"].target_weight() == 0


async def test_a_static_relay_still_refuses_a_different_target(relay) -> None:
    relay()
    relay.state["proxy"].cds_path = None  # an Envoy set up the old way (no cluster file)
    error = await refused()
    assert error.type == "TargetMismatch" and "static" in error.message
    assert address_in(relay.cds) == ORIGINAL


@pytest.mark.parametrize(
    "endpoint",
    ["http://34.64.199.27:8080", "http://192.168.1.10:8080", "http://target.example:8080"],
)
async def test_a_target_outside_the_allowed_range_or_a_name_is_refused(relay, endpoint) -> None:
    relay()
    error = await refused({**HANDOVER, "external_endpoint": endpoint})
    assert error.type == "TargetMismatch" and "not allowed" in error.message
    assert address_in(relay.cds) == ORIGINAL


async def test_the_allowed_range_can_be_widened_by_the_operator(relay, monkeypatch) -> None:
    relay()
    monkeypatch.setenv("TRAFFIC_TARGET_ALLOWED_CIDRS", "10.200.0.0/24, 34.64.199.0/24")
    await prepare({**HANDOVER, "external_endpoint": "http://34.64.199.27:8080"})
    assert address_in(relay.cds) == ("34.64.199.27", 8080)


async def test_a_target_that_is_already_receiving_traffic_is_never_changed(relay) -> None:
    relay()
    relay.state["proxy"].apply({"origin": 90, "target": 10})
    error = await refused()
    assert error.type == "TargetMismatch" and "already" in error.message
    assert address_in(relay.cds) == ORIGINAL


async def test_a_new_target_envoy_never_activates_stops_the_shift(relay) -> None:
    relay(never_active=True)
    error = await refused()
    assert error.type == "TargetMismatch" and "did not become active" in error.message


async def test_a_cluster_file_envoy_rejects_is_reported_as_not_confirmed(relay) -> None:
    relay(reject_cds=True)
    error = await refused()
    assert error.type == "ApplyNotConfirmed"


async def test_a_handover_without_an_address_is_refused(relay) -> None:
    relay()
    error = await refused({**HANDOVER, "external_endpoint": "http://"})
    assert error.type in {"TargetMismatch", "HandoverNotReady"}


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("10.200.0.2", True),
        ("10.200.1.2", False),
        ("34.64.199.27", False),
        ("::1", False),
        ("relay.local", False),
        ("", False),
    ],
)
def test_the_default_allowed_range_is_the_tunnel_range(host, expected, monkeypatch) -> None:
    monkeypatch.delenv("TRAFFIC_TARGET_ALLOWED_CIDRS", raising=False)
    assert target_allowed(host) is expected


# ---- 이미 깔린 중계 서버를 동적으로 바꾸기 ----


def test_an_installed_static_bootstrap_becomes_dynamic_without_changing_where_traffic_goes() -> (
    None
):
    from cmp_traffic.relay import to_dynamic_bootstrap

    static = render_bootstrap(origin=("alb.example", 80), target=ORIGINAL, health_path="/healthz")
    new_yaml, cds_json = to_dynamic_bootstrap(static, "/etc/envoy/cds/cds.json")
    doc = yaml.safe_load(new_yaml)
    assert [c["name"] for c in doc["static_resources"]["clusters"]] == ["cluster_origin"]
    assert (
        doc["dynamic_resources"]["cds_config"]["path_config_source"]["path"]
        == "/etc/envoy/cds/cds.json"
    )
    assert doc["admin"] == yaml.safe_load(static)["admin"]  # everything else is untouched
    (cluster,) = json.loads(cds_json)["resources"]
    socket = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]
    assert (socket["socket_address"]["address"], socket["socket_address"]["port_value"]) == ORIGINAL
    assert cluster["health_checks"][0]["http_health_check"]["path"] == "/healthz"


def test_converting_twice_or_a_bootstrap_without_a_target_is_refused() -> None:
    from cmp_traffic.relay import to_dynamic_bootstrap

    static = render_bootstrap(origin=("alb.example", 80), target=ORIGINAL)
    new_yaml, _ = to_dynamic_bootstrap(static, "/etc/envoy/cds/cds.json")
    with pytest.raises(ValueError, match="already dynamic"):
        to_dynamic_bootstrap(new_yaml, "/etc/envoy/cds/cds.json")
    broken = yaml.safe_load(static)
    broken["static_resources"]["clusters"] = broken["static_resources"]["clusters"][:1]
    with pytest.raises(ValueError, match="no cluster_target"):
        to_dynamic_bootstrap(yaml.safe_dump(broken), "/etc/envoy/cds/cds.json")
