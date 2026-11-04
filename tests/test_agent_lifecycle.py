"""Ticket 15 (#69): Agent last-seen, operator revocation and cluster binding at registration."""

from datetime import UTC, datetime, timedelta

import pytest
from cmp_api.agent_registry import (
    AgentRegistrationError,
    RegisteredAgent,
    authenticate_agent,
    create_enrollment,
    enqueue_job,
    poll_job,
    redeem_enrollment,
    revoke_agent,
)
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.routes.agents import get_session
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

ADMIN = {"X-Enrollment-Admin-Key": "operator-only-test-key"}
WORK = {"kind": "SANDBOX_TEST", "work_ref": {"id": "work-a", "version": "1"}}


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_enrollment_admin_key", "operator-only-test-key")
    app.dependency_overrides[get_session] = lambda: session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def register(session, agent_id="agent-a", cluster_uid="cluster-a", issue_uid="cluster-a", now=None):
    token = create_enrollment(
        session, owner_id="owner-a", agent_id=agent_id, cluster_uid=issue_uid, now=now
    )
    return redeem_enrollment(
        session, token=token, agent_id=agent_id, cluster_uid=cluster_uid, now=now
    )


def test_enrollment_without_cluster_binds_the_uid_reported_at_registration(session):
    credential = register(session, cluster_uid="uid-reported-by-agent", issue_uid=None)
    agent = authenticate_agent(session, agent_id="agent-a", credential=credential)
    assert agent.cluster_uid == "uid-reported-by-agent"


def test_enrollment_with_cluster_still_rejects_a_different_uid(session):
    token = create_enrollment(
        session, owner_id="owner-a", agent_id="agent-a", cluster_uid="cluster-a"
    )
    with pytest.raises(AgentRegistrationError):
        redeem_enrollment(session, token=token, agent_id="agent-a", cluster_uid="cluster-b")
    with pytest.raises(AgentRegistrationError):
        redeem_enrollment(session, token=token, agent_id="agent-a", cluster_uid="")


def test_authenticated_requests_record_last_seen(session):
    issued = datetime(2026, 11, 5, 9, tzinfo=UTC)
    credential = register(session, now=issued)
    assert session.get(RegisteredAgent, "agent-a").last_seen_at is None
    later = issued + timedelta(minutes=3)
    authenticate_agent(session, agent_id="agent-a", credential=credential, now=later)
    assert session.get(RegisteredAgent, "agent-a").last_seen_at == int(later.timestamp())


def test_revoked_agent_cannot_authenticate_and_keeps_its_jobs(session):
    credential = register(session)
    enqueue_job(session, job_id="job-a", owner_id="owner-a", agent_id="agent-a", payload=WORK)
    revoke_agent(session, agent_id="agent-a")
    with pytest.raises(AgentRegistrationError, match="AGENT_UNAUTHORIZED"):
        poll_job(session, agent_id="agent-a", credential=credential)
    with pytest.raises(AgentRegistrationError, match="AGENT_ID_RESERVED"):
        create_enrollment(session, owner_id="owner-a", agent_id="agent-a", cluster_uid="x")
    with pytest.raises(AgentRegistrationError, match="AGENT_NOT_FOUND"):
        revoke_agent(session, agent_id="missing")


def test_issuance_api_accepts_missing_cluster_uid(client):
    issued = client.post(
        "/agent-enrollments", json={"owner_id": "owner-a", "agent_id": "agent-a"}, headers=ADMIN
    )
    assert issued.status_code == 200
    registered = client.post(
        "/agents/register",
        json={
            "enrollment_token": issued.json()["enrollment_token"],
            "agent_id": "agent-a",
            "cluster_uid": "uid-from-cluster",
        },
    )
    assert registered.status_code == 200
    status = client.get("/agent-admin/agents/agent-a", headers=ADMIN).json()
    assert status["cluster_uid"] == "uid-from-cluster"


def test_operator_status_and_revocation_api(client, session):
    credential = register(session)
    auth = {"Authorization": f"Bearer {credential}"}
    assert client.get("/agent-admin/agents/agent-a").status_code == 403
    assert client.post("/agent-admin/agents/agent-a/revoke").status_code == 403

    before = client.get("/agent-admin/agents/agent-a", headers=ADMIN).json()
    assert before == {
        "agent_id": "agent-a",
        "owner_id": "owner-a",
        "cluster_uid": "cluster-a",
        "last_seen_at": None,
        "revoked": False,
    }
    assert client.post("/agents/agent-a/jobs/claim", headers=auth).status_code == 200
    assert client.get("/agent-admin/agents/agent-a", headers=ADMIN).json()["last_seen_at"]

    assert client.post("/agent-admin/agents/agent-a/revoke", headers=ADMIN).status_code == 200
    assert client.get("/agent-admin/agents/agent-a", headers=ADMIN).json()["revoked"] is True
    assert client.post("/agents/agent-a/jobs/claim", headers=auth).status_code == 403
    assert client.get("/agents/agent-a", headers=auth).status_code == 403
    assert (
        client.post(
            "/agents/agent-a/jobs/job-a/result",
            json={"status": "SUCCEEDED", "code": "OK"},
            headers=auth,
        ).status_code
        == 403
    )
    assert client.get("/agent-admin/agents/missing", headers=ADMIN).status_code == 404
    assert client.post("/agent-admin/agents/missing/revoke", headers=ADMIN).status_code == 404
    # Responses never echo the credential.
    assert credential not in client.get("/agent-admin/agents/agent-a", headers=ADMIN).text
