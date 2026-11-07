"""Provisioning 79: `data.preflight` as the web data-copy screen shows it.

Seam: the `run_network_preflight` Activity with a fake probe executor and an in-memory emitter.
Expected shapes come from the web contract (`cmp_contracts.events.DataPreflightData`) and
ADR-007 2안 (the probe reaches the source DB through the data gateway Service, no `wg` device on
the node), not from the implementation.
"""

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.events import validate_data
from cmp_data import tunnel as wg
from cmp_providers.network_preflight import input_fingerprint
from cmp_worker.activities.network_preflight import NetworkPreflightActivities
from cmp_worker.events import configure_emitter
from temporalio.testing import ActivityEnvironment
from test_network_preflight import FakeExecutor, good_observation, request

MID = "migration-001"


@pytest.fixture
def emitter():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


def gateway_request():
    """What `cmp_data.handoff.preflight_request` builds for ON_PREM (ticket 76 W9)."""
    return request(
        source_db_host=wg.peer_host(MID),
        tunnel_cidrs=["10.210.0.0/24"],
        aws_vpc_cidrs=["10.20.0.0/16"],
        onprem_routed_cidrs=["10.42.0.0/16", "10.43.0.0/16"],
        expected_network_path={"allowed_private_destination_cidrs": ["10.43.0.0/16"]},
    )


def gateway_observation():
    return good_observation(
        resolved_ip="10.43.12.7", node_route={"dev": "eth0", "via": None, "src": "10.42.0.9"}
    )


async def run(req, executor=None):
    activities = NetworkPreflightActivities(lambda _r: executor or FakeExecutor())
    return await ActivityEnvironment().run(
        activities.run_network_preflight, req.model_dump(mode="json")
    )


def preflights(emitter):
    events = [e for e in emitter.events if e.type == "data.preflight"]
    for event in events:
        assert validate_data("data.preflight", event.data) == event.data
    return events


async def test_gateway_path_is_shown_as_wireguard_with_the_gateway_service(emitter):
    await run(gateway_request(), FakeExecutor(gateway_observation()))
    for event in preflights(emitter):
        assert event.data["network"]["path"] == {
            "kind": "wireguard",
            "name": f"{wg.name(MID)}-peer-db",
            "cidr": "10.210.0.0/24",
        }


async def test_node_wg_interface_path_is_unchanged(emitter):
    await run(request())
    for event in preflights(emitter):
        assert event.data["network"]["path"] == {
            "kind": "wireguard",
            "name": "wg0",
            "cidr": "10.99.0.0/24",
        }


async def test_path_without_interface_or_tunnel_range_stays_other(emitter):
    await run(
        request(
            tunnel_cidrs=[],
            expected_network_path={"allowed_private_destination_cidrs": ["10.20.0.0/16"]},
        ),
        FakeExecutor(good_observation(node_route={"dev": "eth0", "via": None, "src": None})),
    )
    for event in preflights(emitter):
        assert event.data["network"]["path"] == {"kind": "other", "name": "", "cidr": None}


async def test_running_is_emitted_once_before_the_probe_then_the_result(emitter):
    seen_before_probe = []

    class Recording(FakeExecutor):
        def probe(self, req):
            seen_before_probe.append([e.data["status"] for e in preflights(emitter)])
            return super().probe(req)

    req = gateway_request()
    result = await run(req, Recording(gateway_observation()))
    assert seen_before_probe == [["RUNNING"]]
    running, final = preflights(emitter)
    assert (running.phase, running.status) == ("data_copy", "running")
    assert running.data["may_start"] is False
    assert running.data["checked_at"] is None
    assert running.data["input_fingerprint"] == input_fingerprint(req)
    assert [(c["name"], c["status"]) for c in running.data["checks"]] == [
        ("cidr_overlap", "PENDING"),
        ("route", "PENDING"),
        ("db_path", "PENDING"),
        ("large_transfer", "PENDING"),
    ]
    assert running.data["network"] == final.data["network"]
    assert running.data["probe_location"] == final.data["probe_location"]
    assert (final.status, final.data["status"]) == ("done", "PASS") == ("done", result["status"])


async def test_running_and_result_keys_do_not_collide_and_reruns_show_running_again(emitter):
    req = gateway_request()
    await run(req, FakeExecutor(gateway_observation()))
    await run(req, FakeExecutor(gateway_observation()))
    assert [e.data["status"] for e in preflights(emitter)] == ["RUNNING", "PASS"] * 2


async def test_failed_running_emit_does_not_stop_the_check(emitter):
    class Broken(InMemoryEmitter):
        def _store_event(self, event):
            raise RuntimeError("store down")

    broken = Broken()
    configure_emitter(broken)
    result = await run(gateway_request(), FakeExecutor(gateway_observation()))
    assert result["status"] == "PASS"
    assert broken.events == []
