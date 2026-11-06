"""Ticket 52 (#187): Agent job deadline, operator cancel and environment teardown jobs.

Seam: the real claim/report service (`poll_job`/`report_job`), `cmp_api.agent_jobs`, the
`execute_on_prem_plan` Activity and the admin HTTP routes, on a file SQLite business DB.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cmp_api.agent_jobs import AgentJobError, cancel_job, create_teardown
from cmp_api.agent_registry import AgentJob, AgentRegistrationError, poll_job, report_job
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.onprem_rollout import (
    LocalArtifactStore,
    RolloutError,
    build_rollout_job,
    dispatch,
    outcome,
    rollout_status,
)
from cmp_api.routes.agent_jobs import get_session as admin_session
from cmp_api.routes.agents import get_artifact_store
from cmp_api.routes.agents import get_session as agent_session
from cmp_contracts.onprem_rollout import rollout_job_id
from cmp_contracts.onprem_teardown import TEARDOWN_MODE, TeardownJob, teardown_job_id
from cmp_providers.provisioning_result import PRECONDITION_CODES
from cmp_worker.activities.onprem_execution import OnPremExecutionActivities
from fastapi.testclient import TestClient
from onprem_rollout_support import approve, build_artifacts, claim_request, plan, register_agent
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
JOB = rollout_job_id("plan-k3s", 1)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("artifacts")
    store = LocalArtifactStore(root / "store")
    return store, build_artifacts(store, root)


@pytest.fixture
def store(built):
    return built[0]


@pytest.fixture
def setup(db, built):
    with db() as session:
        credential = register_agent(session)
        manifest = plan(built[1])
        request = claim_request(approve(session, manifest), manifest)
    return {"credential": credential, "request": request}


def queue(db, store, request, deadline=None):
    with db() as session:
        job, owner = build_rollout_job(session, request, store)
        dispatch(session, job, owner, deadline_at=deadline)
    return job.job_id


def epoch(moment):
    return int(moment.timestamp())


def claim(db, credential, now=NOW):
    with db() as session:
        return poll_job(session, agent_id="agent-a", credential=credential, now=now)


def job_row(db, job_id=JOB):
    with db() as session:
        job = session.get(AgentJob, job_id)
        return job.status, job.result


def finish(db, credential, job_id=JOB, status="SUCCEEDED", code="READY"):
    with db() as session:
        report_job(
            session,
            agent_id="agent-a",
            credential=credential,
            job_id=job_id,
            status=status,
            result={"code": code},
        )


# --- deadline (D2–D4) ---------------------------------------------------------------------


async def test_offline_agent_job_expires_with_the_activity_and_is_never_handed_out(
    db, store, setup
):
    activity = OnPremExecutionActivities(db, store, wait_seconds=0.1, poll_seconds=0.02)
    first = await ActivityEnvironment().run(activity.execute_on_prem_plan, setup["request"])
    assert (first["status"], first["code"]) == ("FAILED", "AGENT_OFFLINE")
    assert job_row(db) == ("EXPIRED", {"code": "AGENT_OFFLINE"})
    # The Agent comes back later (C-04 / N14): nothing is handed out, nothing can be reported.
    assert claim(db, setup["credential"], now=datetime.now(UTC)) is None
    with pytest.raises(AgentRegistrationError, match="INVALID_JOB_STATUS"):
        finish(db, setup["credential"])
    with db() as session:
        assert rollout_status(session, "plan-k3s", 1)["state"] == "EXPIRED"
    again = await ActivityEnvironment().run(activity.execute_on_prem_plan, setup["request"])
    assert (again["status"], again["code"]) == ("FAILED", "AGENT_OFFLINE")


def test_claim_refuses_a_queued_job_past_its_deadline(db, store, setup):
    queue(db, store, setup["request"], deadline=epoch(NOW))
    assert claim(db, setup["credential"], now=NOW) is None
    assert job_row(db) == ("EXPIRED", {"code": "JOB_EXPIRED"})
    with db() as session:
        result = outcome(session.get(AgentJob, JOB))
    assert (result["status"], result["code"]) == ("FAILED", "JOB_EXPIRED")
    assert {"JOB_EXPIRED", "JOB_CANCELLED", "AGENT_OFFLINE", "TEARDOWN_IN_PROGRESS"} <= (
        PRECONDITION_CODES
    )


def test_claim_tells_the_agent_how_long_it_may_still_start(db, store, setup):
    queue(db, store, setup["request"], deadline=epoch(NOW + timedelta(seconds=90)))
    claimed = claim(db, setup["credential"], now=NOW)
    assert claimed["job_id"] == JOB and claimed["expires_in_seconds"] == 90
    # Jobs without a deadline (older dispatches, Network Preflight) carry none.
    finish(db, setup["credential"])


def test_reclaimed_running_job_past_its_deadline_is_closed_for_inspection(db, store, setup):
    queue(db, store, setup["request"], deadline=epoch(NOW + timedelta(seconds=60)))
    assert claim(db, setup["credential"], now=NOW)["job_id"] == JOB
    assert claim(db, setup["credential"], now=NOW + timedelta(seconds=30))["job_id"] == JOB
    assert claim(db, setup["credential"], now=NOW + timedelta(seconds=61)) is None
    assert job_row(db) == ("UNKNOWN", {"code": "JOB_EXPIRED"})


def test_job_without_deadline_is_claimed_as_before(db, store, setup):
    queue(db, store, setup["request"])
    claimed = claim(db, setup["credential"], now=NOW + timedelta(days=30))
    assert claimed["job_id"] == JOB and "expires_in_seconds" not in claimed


# --- cancel (D5) --------------------------------------------------------------------------


def test_cancelled_queued_job_never_runs(db, store, setup):
    queue(db, store, setup["request"])
    with db() as session:
        cancel_job(session, JOB, now=NOW)
    assert job_row(db) == ("CANCELLED", {"code": "JOB_CANCELLED"})
    assert claim(db, setup["credential"]) is None
    with db() as session:
        result = outcome(session.get(AgentJob, JOB))
        assert rollout_status(session, "plan-k3s", 1)["state"] == "CANCELLED"
    assert (result["status"], result["code"]) == ("FAILED", "JOB_CANCELLED")


def test_cancel_of_a_running_job_stops_a_reclaim_but_keeps_an_in_flight_report(db, store, setup):
    queue(db, store, setup["request"])
    claim(db, setup["credential"])
    with db() as session:
        cancel_job(session, JOB, now=NOW)
    assert job_row(db)[0] == "RUNNING"
    # The Agent finishing the Helm step it was already in still reports what happened.
    finish(db, setup["credential"], status="FAILED", code="WORKLOAD_NOT_READY")
    assert job_row(db) == ("FAILED", {"code": "WORKLOAD_NOT_READY"})


def test_cancelled_running_job_is_not_resumed_after_an_agent_restart(db, store, setup):
    queue(db, store, setup["request"])
    claim(db, setup["credential"])
    with db() as session:
        cancel_job(session, JOB, now=NOW)
    assert claim(db, setup["credential"]) is None
    assert job_row(db) == ("UNKNOWN", {"code": "JOB_CANCELLED"})


def test_finished_or_preflight_jobs_cannot_be_cancelled(db, store, setup):
    queue(db, store, setup["request"])
    claim(db, setup["credential"])
    finish(db, setup["credential"])
    with db() as session:
        with pytest.raises(AgentJobError, match="JOB_ALREADY_FINISHED"):
            cancel_job(session, JOB)
        with pytest.raises(AgentJobError, match="JOB_NOT_FOUND"):
            cancel_job(session, "missing")
        session.add(
            AgentJob(
                id="preflight-x",
                owner_id="owner-a",
                agent_id="agent-a",
                payload={"mode": "NETWORK_PREFLIGHT"},
                status="QUEUED",
            )
        )
        session.commit()
        with pytest.raises(AgentJobError, match="CANCEL_NOT_SUPPORTED"):
            cancel_job(session, "preflight-x")


# --- teardown (D7–D10) --------------------------------------------------------------------


def deployed(db, store, setup):
    queue(db, store, setup["request"])
    claim(db, setup["credential"])
    finish(db, setup["credential"])


def test_teardown_targets_only_what_the_rollout_job_deployed(db, store, setup):
    deployed(db, store, setup)
    with db() as session:
        row = create_teardown(session, JOB, delete_data=False, now=NOW)
        job = TeardownJob.model_validate(row.payload)
        assert row.deadline_at == epoch(NOW) + 3600
    assert job.mode == TEARDOWN_MODE and job.job_id == teardown_job_id(JOB, False)
    assert (job.agent_id, job.cluster_uid) == ("agent-a", "cluster-a")
    assert (job.namespace, job.environment_id) == ("cmp-prod-demo", "onprem-prod-001")
    assert job.releases == ["demo-app", "demo-db"]
    assert (job.database_release, job.database_secret) == ("demo-db", "demo-db-auth")
    assert job.delete_data is False and job.source_job_id == JOB
    # Same request again: the same job, not a second one.
    with db() as session:
        assert create_teardown(session, JOB, delete_data=False, now=NOW).id == job.job_id
        assert create_teardown(session, JOB, delete_data=True, now=NOW).id != job.job_id


def test_teardown_waits_for_no_rollout_and_blocks_new_rollouts(db, store, built, setup):
    queue(db, store, setup["request"])
    with db() as session, pytest.raises(AgentJobError, match="ROLLOUT_IN_PROGRESS"):
        create_teardown(session, JOB, delete_data=False)
    claim(db, setup["credential"])
    finish(db, setup["credential"])
    with db() as session:
        create_teardown(session, JOB, delete_data=False)
        newer = plan(built[1], revision=2)
        request = claim_request(approve(session, newer), newer)
        with pytest.raises(RolloutError, match="TEARDOWN_IN_PROGRESS"):
            build_rollout_job(session, request, store)
    with db() as session, pytest.raises(AgentJobError, match="TEARDOWN_SOURCE_INVALID"):
        create_teardown(session, "missing", delete_data=False)


def test_queued_teardown_is_claimed_before_other_work(db, store, built, setup):
    deployed(db, store, setup)
    with db() as session:
        teardown = create_teardown(session, JOB, delete_data=False, now=NOW).id
        session.add(
            AgentJob(
                id="aaaa-preflight",
                owner_id="owner-a",
                agent_id="agent-a",
                payload={"mode": "NETWORK_PREFLIGHT"},
                status="QUEUED",
            )
        )
        session.commit()
    claimed = claim(db, setup["credential"], now=NOW)
    assert claimed["job_id"] == teardown and claimed["expires_in_seconds"] == 3600


def test_failed_teardown_can_be_requested_again_and_success_is_kept(db, store, setup):
    deployed(db, store, setup)
    with db() as session:
        tid = create_teardown(session, JOB, delete_data=True, now=NOW).id
    claim(db, setup["credential"], now=NOW)
    finish(db, setup["credential"], job_id=tid, status="UNKNOWN", code="TEARDOWN_INCOMPLETE")
    with db() as session:
        again = create_teardown(session, JOB, delete_data=True, now=NOW + timedelta(hours=2))
        assert (again.status, again.result) == ("QUEUED", None)
        assert again.deadline_at == epoch(NOW + timedelta(hours=2)) + 3600
    claim(db, setup["credential"], now=NOW + timedelta(hours=2))
    finish(db, setup["credential"], job_id=tid, code="TORN_DOWN")
    with db() as session:
        kept = create_teardown(session, JOB, delete_data=True)
        assert (kept.status, kept.result) == ("SUCCEEDED", {"code": "TORN_DOWN"})


def test_revoked_agent_gets_no_teardown(db, store, setup):
    deployed(db, store, setup)
    from cmp_api.agent_registry import revoke_agent

    with db() as session:
        revoke_agent(session, agent_id="agent-a")
        with pytest.raises(AgentJobError, match="AGENT_REVOKED"):
            create_teardown(session, JOB, delete_data=False)


# --- admin HTTP routes --------------------------------------------------------------------


@pytest.fixture
def client(db, store, monkeypatch):
    def session():
        with db() as value:
            yield value

    monkeypatch.setattr(settings, "agent_enrollment_admin_key", "admin")
    app.dependency_overrides[agent_session] = session
    app.dependency_overrides[admin_session] = session
    app.dependency_overrides[get_artifact_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


ADMIN = {"X-Enrollment-Admin-Key": "admin"}


def test_admin_routes_cancel_tear_down_and_show_jobs(db, store, setup, client):
    queue(db, store, setup["request"], deadline=epoch(datetime.now(UTC)) + 600)
    assert client.post(f"/agent-admin/jobs/{JOB}/cancel").status_code == 403
    agent = {"Authorization": f"Bearer {setup['credential']}"}
    claimed = client.post("/agents/agent-a/jobs/claim", headers=agent).json()["job"]
    assert 0 < claimed["expires_in_seconds"] <= 600
    cancelled = client.post(f"/agent-admin/jobs/{JOB}/cancel", headers=ADMIN)
    assert cancelled.status_code == 200
    assert cancelled.json() == {
        "job_id": JOB,
        "mode": "PRODUCTION_ROLLOUT",
        "agent_id": "agent-a",
        "status": "RUNNING",
        "code": None,
        "cancel_requested": True,
    }
    sha = claimed["payload"]["database"]["values"]["sha256"]
    blocked = client.get(f"/agents/agent-a/jobs/{JOB}/artifacts/{sha}", headers=agent)
    assert (blocked.status_code, blocked.json()["detail"]) == (409, "JOB_CANCELLED")
    report = {"status": "FAILED", "code": "ARTIFACT_UNAVAILABLE"}
    client.post(f"/agents/agent-a/jobs/{JOB}/result", headers=agent, json=report)
    teardown = client.post(
        f"/agent-admin/jobs/{JOB}/teardown", headers=ADMIN, json={"delete_data": False}
    )
    assert teardown.status_code == 201, teardown.text
    tid = teardown.json()["job_id"]
    assert teardown.json()["mode"] == TEARDOWN_MODE and teardown.json()["status"] == "QUEUED"
    claimed = client.post("/agents/agent-a/jobs/claim", headers=agent).json()["job"]
    assert claimed["job_id"] == tid and claimed["payload"]["mode"] == TEARDOWN_MODE
    done = client.post(
        f"/agents/agent-a/jobs/{tid}/result",
        headers=agent,
        json={"status": "SUCCEEDED", "code": "TORN_DOWN"},
    )
    assert done.status_code == 202
    shown = client.get(f"/agent-admin/jobs/{tid}", headers=ADMIN).json()
    assert (shown["status"], shown["code"]) == ("SUCCEEDED", "TORN_DOWN")
    assert client.get("/agent-admin/jobs/missing", headers=ADMIN).status_code == 404
    finished = client.post(f"/agent-admin/jobs/{tid}/cancel", headers=ADMIN)
    assert (finished.status_code, finished.json()["detail"]) == (409, "JOB_ALREADY_FINISHED")
    unknown = client.post(
        "/agent-admin/jobs/missing/teardown", headers=ADMIN, json={"delete_data": True}
    )
    assert (unknown.status_code, unknown.json()["detail"]) == (409, "TEARDOWN_SOURCE_INVALID")
