"""MNP-01 seam: run_network_preflight(request, executor) with a fake probe executor (N8).

Expected verdicts come from MNP-001~006 and decisions N1~N8, not from the implementation.
"""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from cmp_contracts.network_preflight import NetworkPreflightRequest, ProbeObservation
from cmp_providers.network_preflight import (
    ProbeLocationUnavailable,
    check_cidr_overlap,
    input_fingerprint,
    may_start_data_migration,
    run_network_preflight,
)
from pydantic import ValidationError

SECRET_VALUE = "replication-password-never-in-results"


def request(**changes) -> NetworkPreflightRequest:
    data = {
        "migration_id": "migration-001",
        "source_db_host": "10.20.3.15",
        "source_db_port": 5432,
        "tls_mode": "require",
        "database": "demoapp",
        "db_auth_ref": {
            "secret_name": "source-db-auth",
            "username_key": "user",
            "password_key": "password",
        },
        "aws_vpc_cidrs": ["10.20.0.0/16"],
        "onprem_routed_cidrs": ["192.168.10.0/24", "10.42.0.0/16"],
        "tunnel_cidrs": ["10.99.0.0/24"],
        "expected_network_path": {
            "allowed_private_destination_cidrs": ["10.20.0.0/16"],
            "tunnel_interface": "wg0",
        },
        "probe_target": {
            "agent_id": "agent-001",
            "cluster_uid": "k3s-uid-001",
            "namespace": "cmp-migration",
            "node_name": "node-a",
        },
    }
    data.update(changes)
    return NetworkPreflightRequest.model_validate(data)


def good_observation(**changes) -> ProbeObservation:
    data = {
        "node": "node-a",
        "resolved_ip": "10.20.3.15",
        "node_route": {"dev": "wg0", "via": None, "src": "10.99.0.2"},
        "db_stage_failed": None,
        "transfer": {"bytes": 4 * 1024 * 1024, "seconds": 1.4, "status": "COMPLETE"},
        "mtu": {"probe": "DF_1400_OK"},
    }
    data.update(changes)
    return ProbeObservation.model_validate(data)


class FakeExecutor:
    def __init__(self, observation=None, error=None):
        self.observation = observation or good_observation()
        self.error = error
        self.calls = []

    def probe(self, req: NetworkPreflightRequest) -> ProbeObservation:
        self.calls.append(req)
        if self.error:
            raise self.error
        return self.observation


def statuses(result) -> dict[str, tuple[str, str | None]]:
    return {c.name: (c.status, c.code) for c in result.checks}


NOW = datetime(2026, 11, 5, tzinfo=UTC)


def test_all_checks_pass_and_start_is_allowed():
    req = request()
    result = run_network_preflight(req, FakeExecutor(), now=NOW)
    assert result.status == "PASS"
    assert set(statuses(result)) == {
        "check_cidr_overlap",
        "check_route",
        "probe_db_path",
        "probe_large_transfer",
    }
    assert {s for s, _ in statuses(result).values()} == {"PASS"}
    assert result.tested_endpoint == "10.20.3.15:5432"
    assert result.probe_location.model_dump() == {
        "agent_id": "agent-001",
        "cluster_uid": "k3s-uid-001",
        "namespace": "cmp-migration",
        "node": "node-a",
    }
    assert result.checked_at == NOW
    assert result.input_fingerprint == input_fingerprint(req)
    assert may_start_data_migration(result, req) == (True, [])


# --- MNP-002 CIDR ----------------------------------------------------------------------------


def test_cidr_overlap_between_aws_and_onprem_routes_is_reported():
    check = check_cidr_overlap(request(onprem_routed_cidrs=["10.20.128.0/17"]))
    assert (check.status, check.code) == ("FAIL", "CIDR_OVERLAP")
    assert check.evidence["conflicts"] == [
        {"a": "10.20.0.0/16", "a_role": "aws_vpc", "b": "10.20.128.0/17", "b_role": "onprem_routed"}
    ]


def test_subnets_inside_the_same_aws_vpc_are_not_conflicts():
    check = check_cidr_overlap(request(aws_vpc_cidrs=["10.20.0.0/16", "10.20.8.0/24"]))
    assert check.status == "PASS"


def test_tunnel_overlap_with_onprem_routes_is_reported():
    check = check_cidr_overlap(request(tunnel_cidrs=["192.168.10.0/25"]))
    assert (check.status, check.code) == ("FAIL", "CIDR_OVERLAP")


def test_missing_required_cidrs_is_unknown_not_pass():
    check = check_cidr_overlap(request(onprem_routed_cidrs=[]))
    assert (check.status, check.code) == ("UNKNOWN", "CIDR_INPUT_MISSING")


# --- MNP-003 route ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        ({"node_route": None}, ("UNKNOWN", "ROUTE_UNVERIFIED")),
        ({"node_route": {"dev": None, "via": None, "src": None}}, ("FAIL", "NO_ROUTE")),
        (
            {"node_route": {"dev": "eth0", "via": "192.168.10.1", "src": "192.168.10.5"}},
            ("FAIL", "UNEXPECTED_PATH"),
        ),
        ({"resolved_ip": "3.36.10.10"}, ("FAIL", "DESTINATION_NOT_ALLOWED")),
    ],
)
def test_route_failures_and_unverifiable_paths_block(observation, expected):
    result = run_network_preflight(
        request(), FakeExecutor(good_observation(**observation)), now=NOW
    )
    assert statuses(result)["check_route"] == expected
    assert result.status == expected[0]
    assert may_start_data_migration(result, request())[0] is False


def test_public_internet_detour_is_never_reported_as_pass():
    # DNS answers with a public address even though the TCP/DB path works.
    obs = good_observation(
        resolved_ip="52.78.84.207",
        node_route={"dev": "eth0", "via": "192.168.10.1", "src": "192.168.10.5"},
    )
    result = run_network_preflight(request(), FakeExecutor(obs), now=NOW)
    assert result.status == "FAIL"


# --- MNP-004 DB path -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage", ["DNS_FAILED", "TCP_UNREACHABLE", "TLS_FAILED", "AUTH_FAILED", "QUERY_FAILED"]
)
def test_db_path_failures_are_distinguished(stage):
    obs = good_observation(db_stage_failed=stage, transfer=None)
    if stage == "DNS_FAILED":
        obs = good_observation(
            db_stage_failed=stage, transfer=None, resolved_ip=None, node_route=None
        )
    result = run_network_preflight(request(), FakeExecutor(obs), now=NOW)
    assert statuses(result)["probe_db_path"] == ("FAIL", stage)
    # Transfer never ran: not claimed as passed.
    assert statuses(result)["probe_large_transfer"][0] in ("UNKNOWN",)
    assert result.status == "FAIL"


# --- MNP-005 transfer ------------------------------------------------------------------------


def test_transfer_interrupted_after_64_kib_is_a_failure_with_evidence():
    obs = good_observation(transfer={"bytes": 70_000, "seconds": 30.0, "status": "INTERRUPTED"})
    result = run_network_preflight(request(), FakeExecutor(obs), now=NOW)
    check = next(c for c in result.checks if c.name == "probe_large_transfer")
    assert (check.status, check.code) == ("FAIL", "TRANSFER_INTERRUPTED")
    assert check.evidence["bytes_received"] == 70_000
    assert check.evidence["stopped_after_64kib"] is True
    assert result.status == "FAIL"


def test_short_or_timed_out_transfers_fail():
    for transfer, code in (
        ({"bytes": 1000, "seconds": 1.0, "status": "COMPLETE"}, "TRANSFER_SHORT"),
        ({"bytes": 2_000_000, "seconds": 60.0, "status": "TIMEOUT"}, "TRANSFER_TIMEOUT"),
    ):
        result = run_network_preflight(
            request(), FakeExecutor(good_observation(transfer=transfer)), now=NOW
        )
        assert statuses(result)["probe_large_transfer"] == ("FAIL", code)


def test_icmp_blocked_mtu_probe_alone_does_not_fail():
    obs = good_observation(mtu={"probe": "ICMP_BLOCKED"})
    result = run_network_preflight(request(), FakeExecutor(obs), now=NOW)
    assert result.status == "PASS"
    check = next(c for c in result.checks if c.name == "probe_large_transfer")
    assert check.evidence["mtu"] == "ICMP_BLOCKED"


# --- MNP-006 unavailable location, reuse, secrets -----------------------------------------


def test_unreachable_probe_location_is_unknown_and_blocks():
    result = run_network_preflight(
        request(), FakeExecutor(error=ProbeLocationUnavailable()), now=NOW
    )
    assert result.status == "UNKNOWN"
    assert statuses(result)["probe_db_path"] == ("UNKNOWN", "PROBE_LOCATION_UNAVAILABLE")
    assert statuses(result)["check_cidr_overlap"][0] == "PASS"  # static check still ran
    assert may_start_data_migration(result, request()) == (False, ["PREFLIGHT_NOT_PASSED"])


def test_cidr_failure_skips_live_probes():
    executor = FakeExecutor()
    result = run_network_preflight(request(tunnel_cidrs=["10.20.0.0/24"]), executor, now=NOW)
    assert result.status == "FAIL"
    assert executor.calls == []
    assert statuses(result)["probe_db_path"] == ("UNKNOWN", "NOT_RUN_AFTER_STATIC_FAILURE")


@pytest.mark.parametrize(
    "change",
    [
        {"source_db_host": "10.20.3.16"},
        {"source_db_port": 6432},
        {"tls_mode": "verify-full"},
        {"tunnel_cidrs": ["10.98.0.0/24"]},
        {
            "probe_target": {
                "agent_id": "agent-001",
                "cluster_uid": "k3s-uid-001",
                "namespace": "other",
                "node_name": "node-a",
            }
        },
    ],
)
def test_previous_pass_is_not_reused_after_path_or_endpoint_changes(change):
    old = request()
    result = run_network_preflight(old, FakeExecutor(), now=NOW)
    assert may_start_data_migration(result, old) == (True, [])
    changed = request(**change)
    assert may_start_data_migration(result, changed) == (False, ["PREFLIGHT_INPUT_CHANGED"])


def test_results_contain_no_secret_values_or_payload():
    result = run_network_preflight(request(), FakeExecutor(), now=NOW)
    text = result.model_dump_json()
    assert SECRET_VALUE not in text
    assert "source-db-auth" not in text  # Secret name stays in the request, not the result
    assert "xxxx" not in text


def test_request_limits_are_enforced():
    with pytest.raises(ValidationError):
        request(transfer_bytes=65 * 1024 * 1024)
    with pytest.raises(ValidationError):
        request(timeout_seconds=601)
    with pytest.raises(ValidationError):
        request(tls_mode="prefer")
    with pytest.raises(ValidationError):
        request(source_db_host="db; rm -rf /")
    assert request().transfer_bytes == 4 * 1024 * 1024
    assert request().timeout_seconds == 60


def test_fingerprint_is_stable_and_ignores_nothing_on_the_path():
    a, b = request(), request()
    assert input_fingerprint(a) == input_fingerprint(b)
    bigger = deepcopy(a.model_dump(mode="json"))
    bigger["transfer_bytes"] = 8 * 1024 * 1024
    assert input_fingerprint(NetworkPreflightRequest.model_validate(bigger)) != input_fingerprint(a)


# --- Worker Activity (N1) --------------------------------------------------------------------


async def test_activity_returns_result_dict_and_unconfigured_worker_never_passes(monkeypatch):
    from cmp_worker.activities.network_preflight import (
        NetworkPreflightActivities,
        build_network_preflight_activities,
    )
    from temporalio.testing import ActivityEnvironment

    payload = request().model_dump(mode="json")
    ok = await ActivityEnvironment().run(
        NetworkPreflightActivities(lambda _request: FakeExecutor()).run_network_preflight, payload
    )
    assert ok["status"] == "PASS" and ok["tested_endpoint"] == "10.20.3.15:5432"

    class NoAgents:  # probe_target.agent_id is not a registered on-prem Agent: direct path
        def is_agent(self, agent_id):
            return False

    for key in ("PREFLIGHT_KUBECONFIG", "PREFLIGHT_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    unconfigured = await ActivityEnvironment().run(
        build_network_preflight_activities(NoAgents()).run_network_preflight, payload
    )
    assert unconfigured["status"] == "UNKNOWN"
    assert {c["code"] for c in unconfigured["checks"][1:]} == {"PROBE_LOCATION_UNAVAILABLE"}
