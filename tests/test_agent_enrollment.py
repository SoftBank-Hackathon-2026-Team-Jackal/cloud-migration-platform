"""Owner-scoped registration and outbound polling seam."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from cmp_api.agent_registry import (
    AgentRegistrationError,
    Enrollment,
    RegisteredAgent,
    authenticate_agent,
    create_enrollment,
    enqueue_job,
    poll_job,
    redeem_enrollment,
    report_job,
)
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.routes.agents import get_session
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


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


def test_registration_is_one_use_and_owner_bound(session):
    now = datetime(2026, 11, 3, tzinfo=UTC)
    token = create_enrollment(
        session, owner_id="owner-a", agent_id="agent-a", cluster_uid="cluster-a", now=now
    )
    assert token.startswith("cmp_enroll_")
    agent_key = redeem_enrollment(
        session, token=token, agent_id="agent-a", cluster_uid="cluster-a", now=now
    )
    assert (
        authenticate_agent(session, agent_id="agent-a", credential=agent_key).owner_id == "owner-a"
    )
    assert session.get(Enrollment, hashlib.sha256(token.encode()).hexdigest()).token_digest != token
    assert session.get(RegisteredAgent, "agent-a").credential_digest != agent_key
    with pytest.raises(AgentRegistrationError):
        redeem_enrollment(
            session, token=token, agent_id="agent-a", cluster_uid="cluster-a", now=now
        )
    with pytest.raises(AgentRegistrationError):
        authenticate_agent(session, agent_id="agent-a", credential="wrong")


def test_expired_or_wrong_target_enrollment_is_rejected(session):
    now = datetime(2026, 11, 3, tzinfo=UTC)
    token = create_enrollment(
        session,
        owner_id="owner-a",
        agent_id="agent-a",
        cluster_uid="cluster-a",
        now=now,
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(AgentRegistrationError):
        redeem_enrollment(
            session, token=token, agent_id="agent-b", cluster_uid="cluster-a", now=now
        )
    with pytest.raises(AgentRegistrationError):
        redeem_enrollment(
            session,
            token=token,
            agent_id="agent-a",
            cluster_uid="cluster-a",
            now=now + timedelta(minutes=5),
        )


def test_poll_and_report_cannot_cross_agent_ownership(session):
    now = datetime(2026, 11, 3, tzinfo=UTC)
    agents = {}
    for name in ("a", "b"):
        token = create_enrollment(
            session,
            owner_id=f"owner-{name}",
            agent_id=f"agent-{name}",
            cluster_uid=f"cluster-{name}",
            now=now,
        )
        agents[name] = redeem_enrollment(
            session, token=token, agent_id=f"agent-{name}", cluster_uid=f"cluster-{name}", now=now
        )
    enqueue_job(
        session,
        job_id="job-a",
        owner_id="owner-a",
        agent_id="agent-a",
        payload={"kind": "SANDBOX_TEST", "work_ref": {"id": "work-a", "version": "1"}},
    )
    assert poll_job(session, agent_id="agent-b", credential=agents["b"]) is None
    own = poll_job(session, agent_id="agent-a", credential=agents["a"])
    assert own["job_id"] == "job-a"
    assert poll_job(session, agent_id="agent-a", credential=agents["a"]) == own
    with pytest.raises(AgentRegistrationError):
        report_job(
            session,
            agent_id="agent-b",
            credential=agents["b"],
            job_id="job-a",
            status="SUCCEEDED",
            result={},
        )
    report_job(
        session,
        agent_id="agent-a",
        credential=agents["a"],
        job_id="job-a",
        status="SUCCEEDED",
        result={"code": "OK"},
    )
    report_job(
        session,
        agent_id="agent-a",
        credential=agents["a"],
        job_id="job-a",
        status="SUCCEEDED",
        result={"code": "OK"},
    )
    assert poll_job(session, agent_id="agent-a", credential=agents["a"]) is None


def test_public_agent_api_requires_admin_issuance_and_agent_auth(session, monkeypatch):
    monkeypatch.setattr(settings, "agent_enrollment_admin_key", "operator-only-test-key")
    app.dependency_overrides[get_session] = lambda: session
    try:
        with TestClient(app) as client:
            enrollment_request = {
                "owner_id": "owner-a",
                "agent_id": "agent-a",
                "cluster_uid": "cluster-a",
            }
            assert client.post("/agent-enrollments", json=enrollment_request).status_code == 403
            issued = client.post(
                "/agent-enrollments",
                json=enrollment_request,
                headers={"X-Enrollment-Admin-Key": "operator-only-test-key"},
            )
            assert issued.status_code == 200
            token = issued.json()["enrollment_token"]
            registered = client.post(
                "/agents/register",
                json={"enrollment_token": token, "agent_id": "agent-a", "cluster_uid": "cluster-a"},
            )
            assert registered.status_code == 200
            credential = registered.json()["credential"]
            assert (
                client.post(
                    "/agents/register",
                    json={
                        "enrollment_token": token,
                        "agent_id": "agent-a",
                        "cluster_uid": "cluster-a",
                    },
                ).status_code
                == 403
            )
            enqueue_job(
                session,
                job_id="job-a",
                owner_id="owner-a",
                agent_id="agent-a",
                payload={"kind": "SANDBOX_TEST", "work_ref": {"id": "work-a", "version": "1"}},
            )
            assert client.post("/agents/agent-a/jobs/claim").status_code == 401
            headers = {"Authorization": f"Bearer {credential}"}
            response = client.post("/agents/agent-a/jobs/claim", headers=headers)
            assert response.status_code == 200
            assert response.json()["job"]["job_id"] == "job-a"
            assert client.post("/agents/other/jobs/claim", headers=headers).status_code == 403
            reported = client.post(
                "/agents/agent-a/jobs/job-a/result",
                json={"status": "SUCCEEDED", "code": "OK"},
                headers=headers,
            )
            assert reported.status_code == 202
    finally:
        app.dependency_overrides.clear()
