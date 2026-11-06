"""Ticket 12 seam: approved ON_PREM Plan → execute_on_prem_plan Activity → Agent job transport.

The Agent here is a fake that uses the real claim/download/report service and HTTP boundary.
Real Helm/kubectl effects are covered by the isolated k3d run (scripts/smoke_onprem_rollout.py).
"""

import asyncio

import pytest
from cmp_api.agent_registry import AgentJob, poll_job, report_job
from cmp_api.approvals import publish_plan_revision, register_approver
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.onprem_rollout import LocalArtifactStore, build_rollout_job, dispatch, rollout_status
from cmp_api.routes.agents import get_artifact_store
from cmp_api.routes.agents import get_session as agent_session
from cmp_api.routes.onprem_rollout import get_session as rollout_session
from cmp_contracts.onprem_rollout import ProductionRolloutJob, rollout_job_id
from cmp_worker.activities.onprem_execution import OnPremExecutionActivities
from fastapi.testclient import TestClient
from onprem_rollout_support import (
    approve,
    build_artifacts,
    claim_request,
    plan,
    preview,
    register_agent,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def db(tmp_path):
    # Activities use the DB from worker threads while the fake Agent polls on the event loop.
    # A file DB gives every session its own connection; a shared in-memory StaticPool
    # connection lets one session's commit/rollback cut into another thread's transaction.
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def artifact_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("artifacts")
    store = LocalArtifactStore(root / "store")
    return store, build_artifacts(store, root)


@pytest.fixture
def store(artifact_root):
    return artifact_root[0]


@pytest.fixture
def artifacts(artifact_root):
    return artifact_root[1]


@pytest.fixture
def setup(db, artifacts):
    with db() as session:
        credential = register_agent(session)
        manifest = plan(artifacts)
        record = approve(session, manifest)
        request = claim_request(record, manifest)
    return {"credential": credential, "manifest": manifest, "request": request}


def activities(db, store, wait=0.5):
    return OnPremExecutionActivities(db, store, wait_seconds=wait, poll_seconds=0.02)


def jobs(db):
    with db() as session:
        return list(session.scalars(select(AgentJob)))


def queue(db, store, request):
    """Queue the Revision's job as dispatch does, without the Activity's start deadline (52)."""
    with db() as session:
        job, owner = build_rollout_job(session, request, store)
        dispatch(session, job, owner)


async def run(db, store, request, wait=0.5):
    return await ActivityEnvironment().run(
        activities(db, store, wait).execute_on_prem_plan, request
    )


async def test_unapproved_or_changed_plan_is_never_dispatched(db, store, setup):
    changed = {**setup["request"], "manifest_digest": "0" * 64}
    assert (await run(db, store, changed))["code"] == "APPROVAL_TARGET_MISMATCH"
    missing = {**setup["request"], "approval_id": "00000000-0000-0000-0000-000000000000"}
    assert (await run(db, store, missing))["code"] == "PLAN_NOT_APPROVED"
    wrong_provider = {**setup["request"], "provider": "AWS"}
    assert (await run(db, store, wrong_provider))["code"] == "PROVIDER_MISMATCH"
    assert jobs(db) == []


async def test_tampered_artifact_bytes_block_dispatch(db, tmp_path, artifacts, setup):
    tampered = LocalArtifactStore(tmp_path / "tampered")
    for ref in artifacts.values():
        tampered.write_raw(ref["sha256"], b"tampered")
    outcome = await run(db, tampered, setup["request"])
    assert outcome == {
        "status": "FAILED",
        "code": "ARTIFACT_DIGEST_MISMATCH",
        "resource_refs": {"job_id": rollout_job_id("plan-k3s", 1)},
    }
    assert jobs(db) == []


async def test_agent_target_must_match_registered_agent_and_cluster(db, store, artifacts):
    with db() as session:
        register_agent(session, cluster_uid="other-cluster")
        manifest = plan(artifacts)
        request = claim_request(approve(session, manifest), manifest)
    assert (await run(db, store, request))["code"] == "AGENT_TARGET_MISMATCH"
    assert jobs(db) == []


async def test_offline_agent_job_expires_then_reconnect_within_the_wait_reports_ready(
    db, store, setup
):
    # Ticket 52 (D3): a job nobody claimed before the wait ended never runs later.
    first = await run(db, store, setup["request"], wait=0.1)
    assert (first["status"], first["code"]) == ("FAILED", "AGENT_OFFLINE")
    [job] = jobs(db)
    assert (job.status, job.result) == ("EXPIRED", {"code": "AGENT_OFFLINE"})
    with db() as session:
        assert poll_job(session, agent_id="agent-a", credential=setup["credential"]) is None
        session.delete(session.get(AgentJob, job.id))  # start over as a fresh dispatch
        session.commit()
    queue(db, store, setup["request"])
    [job] = jobs(db)
    assert job.status == "QUEUED"
    payload = ProductionRolloutJob.model_validate(job.payload)
    assert payload.job_id == rollout_job_id("plan-k3s", 1)
    assert payload.approval_id == setup["request"]["approval_id"]
    assert payload.cluster_uid == "cluster-a" and payload.namespace == "cmp-prod-demo"
    assert payload.storage_class == "local-path"
    assert payload.database.password_secret.name == "demo-db-auth"
    assert [ref.name for ref in payload.application.secret_refs] == ["demo-app-env"]
    with db() as session:
        assert rollout_status(session, "plan-k3s", 1)["state"] == "WAITING_AGENT"

    async def agent_reconnects():
        await asyncio.sleep(0.05)
        with db() as session:
            claimed = poll_job(session, agent_id="agent-a", credential=setup["credential"])
            report_job(
                session,
                agent_id="agent-a",
                credential=setup["credential"],
                job_id=claimed["job_id"],
                status="SUCCEEDED",
                result={"code": "READY"},
            )

    second, _ = await asyncio.gather(run(db, store, setup["request"], wait=2), agent_reconnects())
    assert second["status"] == "SUCCEEDED" and second["code"] == "READY"
    assert second["resource_refs"]["database_endpoint"] == "demo-db.cmp-prod-demo.svc:5432"
    assert second["resource_refs"]["application_endpoint"] == "demo-app.cmp-prod-demo.svc:8080"
    # Ticket 13 R4: standard handoff keys from the job and the approved Values bytes.
    refs = second["resource_refs"]
    assert (refs["agent_id"], refs["cluster_uid"]) == ("agent-a", "cluster-a")
    assert refs["db_host"] == "demo-db.cmp-prod-demo.svc" and refs["db_port"] == "5432"
    assert refs["db_secret_ref"] == "demo-db-auth/" + payload.database.password_secret.key
    assert refs["app_internal_endpoint"] == "http://demo-app.cmp-prod-demo.svc:8080"
    assert refs["app_health_path"] == payload.application.health_path
    assert {"db_name", "db_user", "db_version", "db_storage_size_gib", "app_replicas"} <= set(refs)
    assert all(isinstance(v, str) for v in refs.values())
    assert len(jobs(db)) == 1  # retry reused the same Revision job
    with db() as session:
        assert rollout_status(session, "plan-k3s", 1)["state"] == "READY"
    again = await run(db, store, setup["request"])
    assert again["code"] == "READY" and len(jobs(db)) == 1


async def test_failed_agent_check_is_not_ready(db, store, setup):
    queue(db, store, setup["request"])
    with db() as session:
        claimed = poll_job(session, agent_id="agent-a", credential=setup["credential"])
        report_job(
            session,
            agent_id="agent-a",
            credential=setup["credential"],
            job_id=claimed["job_id"],
            status="FAILED",
            result={"code": "PVC_NOT_BOUND"},
        )
    outcome = await run(db, store, setup["request"])
    assert outcome["status"] == "FAILED" and outcome["code"] == "PVC_NOT_BOUND"
    assert "database_endpoint" not in outcome["resource_refs"]
    with db() as session:
        assert rollout_status(session, "plan-k3s", 1) | {"job_id": None} == {
            "state": "FAILED",
            "code": "PVC_NOT_BOUND",
            "job_id": None,
            "agent_id": "agent-a",
        }


async def test_claim_rejects_queued_job_after_new_revision(db, store, artifacts, setup):
    queue(db, store, setup["request"])
    with db() as session:
        newer = plan(artifacts, revision=2)
        publish_plan_revision(session, newer, preview(newer))
        assert poll_job(session, agent_id="agent-a", credential=setup["credential"]) is None
    [job] = jobs(db)
    assert (job.status, job.result) == ("FAILED", {"code": "STALE_REVISION"})


@pytest.fixture
def client(db, store, monkeypatch):
    def session():
        with db() as value:
            yield value

    monkeypatch.setattr(settings, "approval_admin_key", "admin")
    app.dependency_overrides[agent_session] = session
    app.dependency_overrides[rollout_session] = session
    app.dependency_overrides[get_artifact_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


async def test_agent_downloads_only_its_job_artifacts(db, store, artifacts, setup, client):
    queue(db, store, setup["request"])
    headers = {"Authorization": f"Bearer {setup['credential']}"}
    claim = client.post("/agents/agent-a/jobs/claim", headers=headers)
    assert claim.status_code == 200
    job = claim.json()["job"]
    assert job["payload"]["mode"] == "PRODUCTION_ROLLOUT"
    base = f"/agents/agent-a/jobs/{job['job_id']}/artifacts"
    values = job["payload"]["database"]["values"]
    response = client.get(f"{base}/{values['sha256']}", headers=headers)
    assert response.status_code == 200
    assert response.content == store.read_raw(values["sha256"])
    spec = artifacts["spec"]["sha256"]  # stored, but not part of this job
    assert client.get(f"{base}/{spec}", headers=headers).status_code == 404
    assert client.get(f"{base}/{values['sha256']}").status_code == 401
    with db() as session:
        other = register_agent(session, agent_id="agent-b", cluster_uid="cluster-b")
    other_headers = {"Authorization": f"Bearer {other}"}
    assert client.get(f"{base}/{values['sha256']}", headers=other_headers).status_code == 403
    other_base = f"/agents/agent-b/jobs/{job['job_id']}/artifacts"
    assert client.get(f"{other_base}/{values['sha256']}", headers=other_headers).status_code == 404


async def test_rollout_status_route_requires_approver_grant(db, store, setup, client):
    queue(db, store, setup["request"])
    path = "/provisioning/plans/plan-k3s/revisions/1/rollout"
    with db() as session:
        allowed = register_approver(session, approver_id="viewer", migration_ids=["migration-001"])
        denied = register_approver(session, approver_id="outsider", migration_ids=["other"])
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": f"Bearer {denied}"}).status_code == 403
    response = client.get(path, headers={"Authorization": f"Bearer {allowed}"})
    assert response.status_code == 200
    assert response.json() == {
        "plan_id": "plan-k3s",
        "revision": 1,
        "state": "WAITING_AGENT",
        "code": None,
        "job_id": rollout_job_id("plan-k3s", 1),
        "agent_id": "agent-a",
    }


async def test_approval_workflow_dispatches_on_prem_and_records_ready(db, store, setup):
    """After 08: real ProvisioningApprovalWorkflow → claim → execute_on_prem_plan → complete."""
    from cmp_api.approval_delivery import TemporalApprovalDelivery
    from cmp_api.approvals import PlanApproval, execution_refs, execution_status
    from cmp_contracts.queues import PROVISIONING_QUEUE, WORKFLOW_QUEUE, approval_workflow_id
    from cmp_worker.activities.approval import ApprovalActivities
    from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    approval = ApprovalActivities(db)

    async def agent():
        while True:
            await asyncio.sleep(0.05)
            with db() as session:
                claimed = poll_job(session, agent_id="agent-a", credential=setup["credential"])
                if claimed:
                    report_job(
                        session,
                        agent_id="agent-a",
                        credential=setup["credential"],
                        job_id=claimed["job_id"],
                        status="SUCCEEDED",
                        result={"code": "READY"},
                    )
                    return

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
        Worker(
            env.client,
            task_queue=PROVISIONING_QUEUE,
            activities=[
                approval.claim_approved_execution,
                approval.complete_approved_execution,
                activities(db, store, wait=10).execute_on_prem_plan,
            ],
        ),
    ):
        request = setup["request"]
        await TemporalApprovalDelivery(env.client).deliver(
            approval_id=request["approval_id"], plan_id="plan-k3s", revision=1
        )
        handle = env.client.get_workflow_handle(approval_workflow_id("plan-k3s", 1))
        outcome, _ = await asyncio.gather(asyncio.wait_for(handle.result(), 30), agent())
    assert (outcome["status"], outcome["code"]) == ("EXECUTED", "READY")
    with db() as session:
        record = session.get(PlanApproval, request["approval_id"])
        assert execution_status(session, record) == "SUCCEEDED"
        assert execution_refs(session, record)["database_endpoint"] == (
            "demo-db.cmp-prod-demo.svc:5432"
        )
    assert len(jobs(db)) == 1


@pytest.mark.parametrize(
    ("replication", "capable", "code"),
    [
        ({"capable": True, "code": None}, "true", None),
        ({"capable": False, "code": "WAL_LEVEL_NOT_LOGICAL"}, "false", "WAL_LEVEL_NOT_LOGICAL"),
        (None, None, None),  # older Agent: not verified
    ],
)
async def test_agent_replication_check_becomes_handoff_refs(
    db, store, setup, replication, capable, code
):
    """Ticket 27: the Agent reports its replication check with READY; refs carry it."""
    queue(db, store, setup["request"])
    with db() as session:
        claimed = poll_job(session, agent_id="agent-a", credential=setup["credential"])
        report_job(
            session,
            agent_id="agent-a",
            credential=setup["credential"],
            job_id=claimed["job_id"],
            status="SUCCEEDED",
            result={"code": "READY"} | ({"replication": replication} if replication else {}),
        )
    outcome = await run(db, store, setup["request"])
    assert outcome["code"] == "READY"
    refs = outcome["resource_refs"]
    assert refs.get("db_replication_capable") == capable
    assert refs.get("db_replication_code") == code


async def test_replication_is_accepted_only_with_a_ready_report(db, store, setup):
    from cmp_api.agent_registry import AgentRegistrationError

    queue(db, store, setup["request"])
    with db() as session:
        claimed = poll_job(session, agent_id="agent-a", credential=setup["credential"])
        with pytest.raises(AgentRegistrationError, match="INVALID_JOB_RESULT"):
            report_job(
                session,
                agent_id="agent-a",
                credential=setup["credential"],
                job_id=claimed["job_id"],
                status="FAILED",
                result={"code": "PVC_NOT_BOUND", "replication": {"capable": True}},
            )
