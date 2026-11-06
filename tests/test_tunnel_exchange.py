"""Ticket 16 (#70) seam: FastAPI TestClient + SQLite business DB.

Covers Agent public key registration, tunnel IP allocation, Agent tunnel config, relay
registration, the relay-side Agent list (P1), tunnel status and the ON_PREM Data/Traffic handoff
(T2~T13). Real WireGuard (17) and the relay applying the list (Traffic) are out of scope.
"""

import asyncio
import base64
import os
import time

import pytest
from cmp_api.agent_registry import revoke_agent
from cmp_api.approvals import claim_execution, complete_execution
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.provisioning_results import load_result
from cmp_api.routes.agents import get_session
from cmp_api.tunnels import RelayServer
from cmp_worker.activities.approval import ApprovalActivities
from fastapi.testclient import TestClient
from onprem_rollout_support import register_agent
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from temporalio.testing import ActivityEnvironment
from test_provisioning_result import ONPREM_REFS, Approved

ADMIN = {"X-Enrollment-Admin-Key": "operator-only-test-key"}


def wg_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(settings, "agent_enrollment_admin_key", "operator-only-test-key")
    session = Session(engine)
    app.dependency_overrides[get_session] = lambda: session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        session.close()


def agent(db, agent_id="agent-001", cluster_uid="k3s-uid-001") -> dict:
    with db() as s:
        credential = register_agent(s, agent_id=agent_id, cluster_uid=cluster_uid, owner="owner-a")
    return {"Authorization": f"Bearer {credential}"}


def put_key(client, agent_id, headers, key=None):
    return client.put(
        f"/agents/{agent_id}/tunnel/key", json={"public_key": key or wg_key()}, headers=headers
    )


def put_relay(
    client, relay_id="relay-seoul", endpoint="13.124.10.10:51820", key=None, headers=ADMIN
):
    return client.put(
        f"/tunnel-admin/relays/{relay_id}",
        json={"endpoint": endpoint, "public_key": key or wg_key()},
        headers=headers,
    )


# --- T4/T9: key registration and stable tunnel IPs -------------------------------------------


def test_each_agent_gets_a_stable_unique_tunnel_ip(client, db):
    a, b = agent(db), agent(db, "agent-002", "k3s-uid-002")
    first = put_key(client, "agent-001", a)
    assert first.status_code == 200
    assert first.json()["tunnel_ip"] == "10.200.0.2"
    assert put_key(client, "agent-002", b).json()["tunnel_ip"] == "10.200.0.3"
    # Re-registration and key rotation keep the same address.
    rotated = put_key(client, "agent-001", a)
    assert rotated.json()["tunnel_ip"] == "10.200.0.2"


def test_key_registration_returns_the_relays_to_connect_to(client, db):
    headers = agent(db)
    relay_key = wg_key()
    put_relay(client, key=relay_key)
    body = put_key(client, "agent-001", headers).json()
    assert body["relays"] == [
        {"relay_id": "relay-seoul", "endpoint": "13.124.10.10:51820", "public_key": relay_key}
    ]
    assert body["config_version"] == 1
    assert "credential" not in str(body)


def test_only_public_keys_are_accepted(client, db):
    headers = agent(db)
    assert put_key(client, "agent-001", headers, key="not-a-key").status_code == 422
    leaked = client.put(
        "/agents/agent-001/tunnel/key",
        json={"public_key": wg_key(), "private_key": wg_key()},
        headers=headers,
    )
    assert leaked.status_code == 422


def test_agent_can_only_touch_its_own_tunnel_and_revoked_agents_are_refused(client, db):
    a, b = agent(db), agent(db, "agent-002", "k3s-uid-002")
    put_key(client, "agent-001", a)
    assert put_key(client, "agent-001", b).status_code == 403
    assert client.get("/agents/agent-001/tunnel/config", headers=b).status_code == 403
    with db() as s:
        revoke_agent(s, agent_id="agent-001")
    assert put_key(client, "agent-001", a).status_code == 403
    assert client.get("/agents/agent-001/tunnel/config", headers=a).status_code == 403


# --- T3/T8: relay registration and config version --------------------------------------------


def test_relay_registration_returns_credential_once_and_bumps_the_version(client, db):
    created = put_relay(client)
    assert created.status_code == 201
    body = created.json()
    assert body["credential"].startswith("cmp_relay_") and body["config_version"] == 1
    with db() as s:
        row = s.get(RelayServer, "relay-seoul")
        assert body["credential"] not in (row.credential_digest, row.endpoint, row.public_key)
    updated = put_relay(client, endpoint="13.124.10.11:51820")
    assert updated.status_code == 200
    assert updated.json()["credential"] is None  # never shown again
    assert updated.json()["config_version"] == 2


def test_relay_registration_requires_the_operator_key(client):
    assert put_relay(client, headers={}).status_code in (401, 403)
    assert put_relay(client, headers={"X-Enrollment-Admin-Key": "wrong"}).status_code in (401, 403)


def test_agent_config_lists_relays_tunnel_settings_and_exposed_ports(client, db):
    headers = agent(db)
    put_key(client, "agent-001", headers)
    relay_key = wg_key()
    put_relay(client, key=relay_key)

    config = client.get("/agents/agent-001/tunnel/config", headers=headers)
    assert config.status_code == 200
    body = config.json()
    assert body["tunnel_ip"] == "10.200.0.2"
    assert body["relays"] == [
        {"relay_id": "relay-seoul", "endpoint": "13.124.10.10:51820", "public_key": relay_key}
    ]
    assert body["allowed_ips"] == ["10.200.0.0/24"]
    assert (body["mtu"], body["persistent_keepalive"]) == (1420, 25)
    assert body["exposed_ports"] == [5432]  # no READY ON_PREM app yet
    assert body["config_version"] == 1
    assert "owner" not in config.text and "credential" not in config.text


def test_config_without_a_registered_key_is_not_found(client, db):
    headers = agent(db)
    response = client.get("/agents/agent-001/tunnel/config", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "TUNNEL_KEY_NOT_REGISTERED"


# --- T2/T5/T6/T7: tunnel status and the ON_PREM Data handoff ---------------------------------


def onprem_ready(db):
    approved = Approved(db, "ON_PREM", "FULL")
    with db() as s:
        claim_execution(s, approved.approval_id)
        complete_execution(
            s,
            approved.approval_id,
            status="SUCCEEDED",
            code="READY",
            step="execute_on_prem_plan",
            resource_refs=ONPREM_REFS,
        )
    return approved


def handoff(db, consumer, clock=None):
    return asyncio.run(
        ActivityEnvironment().run(
            ApprovalActivities(db, clock=clock).get_migration_handoff,
            {"plan_id": "plan-001", "revision": 1, "consumer": consumer},
        )
    )


def report(client, headers, **body):
    payload = {
        "tunnel_ip": "10.200.0.2",
        "last_handshake_at": int(time.time()),
        "app_health_ok": True,
        "db_connect_ok": True,
    } | body
    return client.post("/agents/agent-001/tunnel/status", json=payload, headers=headers)


def test_onprem_data_handoff_waits_for_a_tunnel_db_check(client, db):
    headers = agent(db)
    put_key(client, "agent-001", headers)
    onprem_ready(db)

    before = handoff(db, "DATA_MIGRATION")
    assert before["proceed"] is False
    assert before["blocked_by"] == ["TUNNEL_DB_NOT_VERIFIED"]
    assert before["database"]["tunnel_endpoint"] is None

    assert report(client, headers, db_connect_ok=False).status_code == 202
    assert handoff(db, "DATA_MIGRATION")["blocked_by"] == ["TUNNEL_DB_NOT_VERIFIED"]

    assert report(client, headers).status_code == 202
    after = handoff(db, "DATA_MIGRATION")
    assert after["proceed"] is True and after["blocked_by"] == []
    assert after["database"]["tunnel_endpoint"] == "10.200.0.2:5432"

    # T6: the stored result itself is never rewritten.
    with db() as s:
        assert load_result(s, "plan-001", 1).database.tunnel_endpoint is None


def test_onprem_traffic_handoff_waits_for_a_tunnel_app_health_check(client, db):
    headers = agent(db)
    put_key(client, "agent-001", headers)
    relay = put_relay(client).json()
    onprem_ready(db)

    before = handoff(db, "TRAFFIC_MIGRATION")
    assert before["proceed"] is False
    assert before["blocked_by"] == ["EXTERNAL_ENDPOINT_UNAVAILABLE"]
    app = before["application"]
    assert (app["external_endpoint"], app["external_host"], app["external_port"]) == (None,) * 3

    report(client, headers, app_health_ok=False)
    assert handoff(db, "TRAFFIC_MIGRATION")["blocked_by"] == ["EXTERNAL_ENDPOINT_UNAVAILABLE"]

    report(client, headers)
    after = handoff(db, "TRAFFIC_MIGRATION")
    assert after["proceed"] is True and after["blocked_by"] == []
    app = after["application"]
    # P2/P5/T10: the relay forwards to the tunnel side as-is (app port, not 80).
    assert app["external_endpoint"] == "http://10.200.0.2:8080"
    assert (app["external_host"], app["external_port"], app["health_path"]) == (
        "10.200.0.2",
        8080,
        "/health",
    )
    assert "13.124.10.10" not in str(after)  # the relay public address is never a field value
    assert relay["credential"] not in str(after)
    with db() as s:
        stored = load_result(s, "plan-001", 1).application
        assert (stored.external_endpoint, stored.external_host) == (None, None)


# --- P1: relay-side Agent list ----------------------------------------------------------------


def relay_list(client, relay_id="relay-seoul", credential=None):
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    return client.get(f"/relays/{relay_id}/agents", headers=headers)


def test_relay_lists_agents_with_keys_tunnel_ips_and_ports(client, db):
    credential = put_relay(client).json()["credential"]
    headers = agent(db)
    agent_key = wg_key()
    put_key(client, "agent-001", headers, key=agent_key)
    onprem_ready(db)

    response = relay_list(client, credential=credential)
    assert response.status_code == 200
    body = response.json()
    assert body["agents"] == [
        {
            "agent_id": "agent-001",
            "public_key": agent_key,
            "tunnel_ip": "10.200.0.2",
            "allowed_ips": ["10.200.0.2/32"],
            "allowed_ports": [5432, 8080],
            "kube_api_port": None,  # ticket 74: off by default
            "revoked": False,
        }
    ]
    assert isinstance(body["list_version"], str) and body["list_version"]
    assert "owner" not in response.text and "cluster_uid" not in response.text


def test_relay_list_version_changes_with_the_list(client, db):
    credential = put_relay(client).json()["credential"]
    empty = relay_list(client, credential=credential).json()
    assert empty["agents"] == []
    headers = agent(db)
    put_key(client, "agent-001", headers)
    first = relay_list(client, credential=credential).json()["list_version"]
    assert first != empty["list_version"]
    assert relay_list(client, credential=credential).json()["list_version"] == first  # stable
    put_key(client, "agent-001", headers)  # key rotation
    assert relay_list(client, credential=credential).json()["list_version"] != first


def test_revoked_agents_stay_listed_with_a_marker_so_the_relay_can_drop_the_peer(client, db):
    credential = put_relay(client).json()["credential"]
    headers = agent(db)
    put_key(client, "agent-001", headers)
    before = relay_list(client, credential=credential).json()
    with db() as s:
        revoke_agent(s, agent_id="agent-001")
    after = relay_list(client, credential=credential).json()
    (entry,) = after["agents"]
    assert entry["agent_id"] == "agent-001" and entry["revoked"] is True
    assert entry["allowed_ports"] == []
    assert after["list_version"] != before["list_version"]


def test_only_an_authenticated_relay_reads_the_agent_list(client, db):
    credential = put_relay(client).json()["credential"]
    other = put_relay(client, relay_id="relay-tokyo", endpoint="13.124.10.20:51820").json()
    assert relay_list(client).status_code == 401
    assert relay_list(client, credential="cmp_relay_wrong").status_code == 403
    assert relay_list(client, relay_id="relay-tokyo", credential=credential).status_code == 403
    assert relay_list(client, relay_id="relay-nope", credential=credential).status_code == 403
    assert (
        relay_list(client, relay_id="relay-tokyo", credential=other["credential"]).status_code
        == 200
    )
    agent_headers = agent(db)
    put_key(client, "agent-001", agent_headers)
    assert relay_list(client, credential=agent_headers["Authorization"][7:]).status_code == 403


def test_exposed_ports_include_ready_onprem_app_ports(client, db):
    headers = agent(db)
    put_key(client, "agent-001", headers)
    onprem_ready(db)
    body = client.get("/agents/agent-001/tunnel/config", headers=headers).json()
    assert body["exposed_ports"] == [5432, 8080]


def test_status_for_another_tunnel_ip_is_rejected(client, db):
    headers = agent(db)
    put_key(client, "agent-001", headers)
    response = report(client, headers, tunnel_ip="10.200.0.9")
    assert response.status_code == 409
    assert response.json()["detail"] == "TUNNEL_IP_MISMATCH"


def test_status_failure_code_is_stored_and_does_not_unblock_the_handoff(client, db):
    """Ticket 17 W8: the Agent reports why a tunnel check failed (code only, optional)."""
    from cmp_api.tunnels import TunnelStatus, tunnel_view

    headers = agent(db)
    put_key(client, "agent-001", headers)
    onprem_ready(db)
    failed = report(
        client, headers, app_health_ok=False, db_connect_ok=False, code="HANDSHAKE_MISSING"
    )
    assert failed.status_code == 202
    with db() as s:
        assert s.get(TunnelStatus, "agent-001").code == "HANDSHAKE_MISSING"
        assert tunnel_view(s, "agent-001")["code"] == "HANDSHAKE_MISSING"
    assert handoff(db, "TRAFFIC_MIGRATION")["proceed"] is False

    assert report(client, headers).status_code == 202  # code omitted = success, stays compatible
    with db() as s:
        assert s.get(TunnelStatus, "agent-001").code is None


@pytest.mark.parametrize("code", ["handshake missing", "X" * 65, "secret=abc"])
def test_status_code_must_be_a_short_upper_case_code(client, db, code):
    headers = agent(db)
    put_key(client, "agent-001", headers)
    assert report(client, headers, code=code).status_code == 422


def test_alembic_migration_matches_tunnel_models(tmp_path, monkeypatch):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    command.upgrade(Config("alembic.ini"), "head")
    migrated = create_engine(url)
    with migrated.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    migrated.dispose()
    tables = ("agent_tunnels", "relay_servers", "tunnel_settings", "agent_tunnel_status")
    assert [d for d in diff if any(t in str(d) for t in tables)] == []
    command.downgrade(Config("alembic.ini"), "20261105_02")


# --- Ticket 25 (#135): tunnel status freshness -----------------------------------------------


def reported_at(db, agent_id="agent-001") -> int:
    from cmp_api.tunnels import TunnelStatus

    with db() as s:
        return s.get(TunnelStatus, agent_id).reported_at


def onprem_verified(client, db, **body):
    headers = agent(db)
    put_key(client, "agent-001", headers)
    onprem_ready(db)
    assert report(client, headers, **body).status_code == 202
    return headers


@pytest.mark.parametrize("consumer", ["DATA_MIGRATION", "TRAFFIC_MIGRATION"])
def test_fresh_tunnel_report_lets_the_onprem_handoff_proceed(client, db, consumer):
    onprem_verified(client, db)
    at = reported_at(db)
    view = handoff(db, consumer, clock=lambda: at + 60)  # F2: TTL 60 s is inclusive
    assert view["proceed"] is True and view["blocked_by"] == []


def test_stale_report_blocks_both_onprem_handoffs_without_tunnel_values(client, db):
    onprem_verified(client, db)
    at = reported_at(db)
    later = lambda: at + 61

    data = handoff(db, "DATA_MIGRATION", clock=later)
    assert data["proceed"] is False and data["blocked_by"] == ["TUNNEL_STATUS_STALE"]
    assert data["database"]["tunnel_endpoint"] is None

    traffic = handoff(db, "TRAFFIC_MIGRATION", clock=later)
    assert traffic["proceed"] is False and traffic["blocked_by"] == ["TUNNEL_STATUS_STALE"]
    app = traffic["application"]
    assert (app["external_endpoint"], app["external_host"], app["external_port"]) == (None,) * 3

    # T6: the stored result itself is never rewritten either way.
    with db() as s:
        stored = load_result(s, "plan-001", 1)
        assert stored.database.tunnel_endpoint is None
        assert (stored.application.external_endpoint, stored.application.external_host) == (
            None,
            None,
        )


def test_stale_handshake_blocks_even_with_a_recent_report(client, db):
    onprem_verified(client, db, last_handshake_at=int(time.time()) - 181)
    at = reported_at(db)
    for consumer in ("DATA_MIGRATION", "TRAFFIC_MIGRATION"):
        view = handoff(db, consumer, clock=lambda: at)
        assert view["blocked_by"] == ["TUNNEL_STATUS_STALE"]


def test_a_new_report_reenables_the_handoff(client, db):
    headers = onprem_verified(client, db)
    first = reported_at(db)
    assert handoff(db, "TRAFFIC_MIGRATION", clock=lambda: first + 3600)["proceed"] is False

    assert report(client, headers).status_code == 202
    again = reported_at(db)
    view = handoff(db, "TRAFFIC_MIGRATION", clock=lambda: again + 1)
    assert view["proceed"] is True
    assert view["application"]["external_host"] == "10.200.0.2"


def test_never_verified_and_failed_checks_keep_their_codes_when_stale(client, db):
    """F3: TUNNEL_STATUS_STALE only replaces a check that passed; failures keep 16's codes."""
    onprem_verified(client, db, app_health_ok=False, db_connect_ok=False)
    at = reported_at(db)
    later = lambda: at + 3600
    assert handoff(db, "DATA_MIGRATION", clock=later)["blocked_by"] == ["TUNNEL_DB_NOT_VERIFIED"]
    assert handoff(db, "TRAFFIC_MIGRATION", clock=later)["blocked_by"] == [
        "EXTERNAL_ENDPOINT_UNAVAILABLE"
    ]


def test_freshness_ttls_are_settings(client, db, monkeypatch):
    onprem_verified(client, db)
    at = reported_at(db)
    monkeypatch.setattr(settings, "tunnel_status_ttl_seconds", 300)
    monkeypatch.setattr(settings, "tunnel_handshake_stale_seconds", 600)
    assert handoff(db, "DATA_MIGRATION", clock=lambda: at + 120)["proceed"] is True
    monkeypatch.setattr(settings, "tunnel_status_ttl_seconds", 30)
    assert handoff(db, "DATA_MIGRATION", clock=lambda: at + 31)["proceed"] is False


def test_tunnel_view_reports_staleness_and_fresh_until():
    from cmp_api.tunnels import TunnelStatus, tunnel_freshness

    status = TunnelStatus(
        agent_id="a",
        tunnel_ip="10.200.0.2",
        last_handshake_at=1000,
        app_health_ok=True,
        db_connect_ok=True,
        reported_at=1100,
    )
    # fresh_until = min(reported_at + 60, last_handshake_at + 180)
    assert tunnel_freshness(status, now=1160) == (False, 1160)
    assert tunnel_freshness(status, now=1161) == (True, 1160)
    status.last_handshake_at = 950
    assert tunnel_freshness(status, now=1130) == (False, 1130)
    assert tunnel_freshness(status, now=1131) == (True, 1130)
    status.last_handshake_at = None
    assert tunnel_freshness(status, now=1100) == (True, None)


# --- Ticket 73 (#299): K3s API port on the Agent tunnel (ADR-007 step 2) ----------------------


def test_kube_api_port_is_off_by_default(client, db):
    headers = agent(db)
    put_key(client, "agent-001", headers)
    onprem_ready(db)
    body = client.get("/agents/agent-001/tunnel/config", headers=headers).json()
    assert body["kube_api_port"] is None
    assert body["exposed_ports"] == [5432, 8080]


def test_enabled_kube_api_port_is_exposed_to_the_agent_and_the_relay(client, db, monkeypatch):
    monkeypatch.setattr(settings, "tunnel_expose_kube_api", True)
    monkeypatch.setattr(settings, "tunnel_kube_api_port", 16443)
    credential = put_relay(client).json()["credential"]
    headers = agent(db)
    put_key(client, "agent-001", headers)
    onprem_ready(db)
    body = client.get("/agents/agent-001/tunnel/config", headers=headers).json()
    assert body["kube_api_port"] == 16443
    assert body["exposed_ports"] == [5432, 8080, 16443]
    listed = relay_list(client, credential=credential).json()["agents"][0]
    assert listed["allowed_ports"] == [5432, 8080, 16443]
    assert listed["kube_api_port"] == 16443  # ticket 74: the relay knows which port is the API


def test_revoked_agent_gets_no_kube_api_port_on_the_relay(client, db, monkeypatch):
    from cmp_api.agent_registry import revoke_agent

    monkeypatch.setattr(settings, "tunnel_expose_kube_api", True)
    credential = put_relay(client).json()["credential"]
    headers = agent(db)
    put_key(client, "agent-001", headers)
    with db() as s:
        revoke_agent(s, agent_id="agent-001")
    listed = relay_list(client, credential=credential).json()["agents"][0]
    assert listed["allowed_ports"] == []
    assert listed["kube_api_port"] is None  # ticket 74: the relay drops the forward


def test_kube_api_check_is_stored_apart_from_the_handoff_checks(client, db):
    from cmp_api.tunnels import tunnel_view

    headers = onprem_verified(client, db, kube_api_ok=False)
    with db() as s:
        view = tunnel_view(s, "agent-001")
    assert view["kube_api_ok"] is False
    assert view["code"] is None  # app/DB handoff stays healthy
    assert handoff(db, "TRAFFIC_MIGRATION")["proceed"] is True
    assert report(client, headers).status_code == 202  # omitted = not exposed
    with db() as s:
        assert tunnel_view(s, "agent-001")["kube_api_ok"] is None
