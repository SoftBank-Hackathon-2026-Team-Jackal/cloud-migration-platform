"""Provisioning 76: ON_PREM(ADR-007 2안) 데이터 입력 → MNP `NetworkPreflightRequest`."""

from __future__ import annotations

import json

import pytest
from cmp_contracts.network_preflight import NetworkPreflightRequest
from cmp_data import tunnel as wg
from cmp_data.handoff import DataMigrationInput, endpoints, preflight_request
from pydantic import ValidationError

MID = "migration-onprem"
SOURCE = {"access": "AWS_EKS", "cluster_name": "cmp-demo-source", "region": "ap-northeast-2",
          "namespace": "chat-source", "pod": "chat-db-0",
          "local_host": "chat-db.chat-source.svc", "dbname": "chat"}  # fmt: skip
PREFLIGHT = {
    "aws_vpc_cidrs": ["10.0.0.0/16"],
    "db_auth_ref": {"secret_name": "cmp-preflight-db", "username_key": "username",
                    "password_key": "password"},
}  # fmt: skip
GIVEN = {
    "source": SOURCE, "app": {}, "settings": {}, "target_kubeconfig_ref": "onprem-k3d",
    "tunnel": {"ingress": "source", "target_egress_cidrs": ["203.0.113.7/32"]},
    "network_preflight": PREFLIGHT,
}  # fmt: skip
VIEW = {
    "proceed": True,
    "common": {"provider": "ON_PREM", "namespace": "chat",
               "cluster": {"kind": "K3s", "agent_id": "office-seoul-01",
                           "cluster_uid": "cluster-seoul"}},
    "database": {"release": "chat-db", "name": "chat", "ready": True},
}  # fmt: skip


def build(given=GIVEN, view=VIEW):
    parsed = DataMigrationInput.model_validate(given)
    _, target, code = endpoints(view, parsed, MID)
    assert code is None
    return preflight_request(view, parsed, MID, target)


def test_request_probes_the_gateway_from_the_target_db_namespace():
    request, code = build()
    assert code is None
    parsed = NetworkPreflightRequest.model_validate_json(json.dumps(request))
    assert parsed.source_db_host == wg.peer_host(MID) and parsed.source_db_port == 5432
    assert (parsed.database, parsed.tls_mode) == ("chat", "disable")
    assert parsed.db_auth_ref.secret_name == "cmp-preflight-db"
    assert parsed.expected_network_path.allowed_private_destination_cidrs == ["10.43.0.0/16"]
    assert parsed.expected_network_path.tunnel_interface is None
    assert parsed.onprem_routed_cidrs == ["10.42.0.0/16", "10.43.0.0/16"]
    assert parsed.tunnel_cidrs == ["10.210.0.0/24"] and parsed.aws_vpc_cidrs == ["10.0.0.0/16"]
    target = parsed.probe_target
    assert (target.agent_id, target.cluster_uid, target.namespace, target.node_name) == (
        "office-seoul-01", "cluster-seoul", "chat", None)  # fmt: skip


def test_settings_can_override_location_and_ranges():
    given = GIVEN | {"network_preflight": PREFLIGHT | {
        "probe_namespace": "cmp-data-tunnel", "node_name": "k3d-cmp-server-0",
        "allowed_private_destination_cidrs": ["10.96.0.0/12"], "tls_mode": "require"}}  # fmt: skip
    request, _ = build(given)
    assert request["probe_target"]["namespace"] == "cmp-data-tunnel"
    assert request["probe_target"]["node_name"] == "k3d-cmp-server-0"
    assert request["tls_mode"] == "require"


def test_no_settings_means_no_preflight():
    assert build({k: v for k, v in GIVEN.items() if k != "network_preflight"}) == (None, None)


def test_missing_agent_identity_blocks():
    view = VIEW | {"common": {**VIEW["common"], "cluster": {"kind": "K3s", "agent_id": "a"}}}
    assert build(view=view) == (None, "NETWORK_PREFLIGHT_TARGET_MISSING")


@pytest.mark.parametrize(
    "change",
    [
        {"network_preflight": {**PREFLIGHT, "aws_vpc_cidrs": []}},
        {"network_preflight": {**PREFLIGHT, "aws_vpc_cidrs": ["10.0.0.1/16"]}},  # host bits
        {"network_preflight": {k: v for k, v in PREFLIGHT.items() if k != "db_auth_ref"}},
        {"network_preflight": {**PREFLIGHT, "password": "never"}},
        {"tunnel": None},  # 검사 주소가 게이트웨이라 터널이 있어야 한다
    ],
)
def test_bad_settings_are_refused_at_input(change):
    with pytest.raises(ValidationError):
        DataMigrationInput.model_validate(GIVEN | change)
