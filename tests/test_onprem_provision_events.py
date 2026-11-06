"""Ticket Provisioning 63: the ON_PREM rollout emits the `provision.resource` and
`provision.check` events the web build screen needs to finish (or to show a failure).

Web rule (cloud-migration-web `ProvisioningStage.tsx`): finished when the last listed resource
(upserted by `id`, first-seen order) has progress 100 and every received check is `done`.
"""

import asyncio

import pytest
from cmp_api.agent_registry import AgentJob, poll_job, report_job
from cmp_api.db import Base
from cmp_api.migration_events import InMemoryEmitter
from cmp_api.onprem_rollout import LocalArtifactStore, build_rollout_job, dispatch
from cmp_contracts.events import validate_data
from cmp_contracts.onprem_rollout import rollout_job_id
from cmp_worker.activities.onprem_execution import OnPremExecutionActivities
from cmp_worker.events import configure_emitter
from onprem_rollout_support import approve, build_artifacts, claim_request, plan, register_agent
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

MIGRATION = "migration-001"
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
def artifact_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("artifacts")
    store = LocalArtifactStore(root / "store")
    return store, build_artifacts(store, root)


@pytest.fixture
def store(artifact_root):
    return artifact_root[0]


@pytest.fixture
def setup(db, artifact_root):
    with db() as session:
        credential = register_agent(session)
        manifest = plan(artifact_root[1])
        record = approve(session, manifest)
        request = claim_request(record, manifest)
    return {"credential": credential, "request": request}


@pytest.fixture
def emitted():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


async def run(db, store, request, wait=0.5):
    activity = OnPremExecutionActivities(db, store, wait_seconds=wait, poll_seconds=0.02)
    return await ActivityEnvironment().run(activity.execute_on_prem_plan, request)


def finish_job(db, store, setup, status, code):
    """The Agent already finished the Revision's job before the Activity runs."""
    with db() as session:
        job, owner = build_rollout_job(session, setup["request"], store)
        dispatch(session, job, owner)
        claimed = poll_job(session, agent_id="agent-a", credential=setup["credential"])
        report_job(
            session,
            agent_id="agent-a",
            credential=setup["credential"],
            job_id=claimed["job_id"],
            status=status,
            result={"code": code},
        )


def provision(emitter):
    events = [e for e in emitter.events if e.type.startswith("provision.")]
    for event in events:  # every stored event passes the web contract
        assert event.migration_id == MIGRATION and event.phase == "provisioning"
        validate_data(event.type, event.data)
    return events


def web_state(events):
    """Same fold as the web reducer: resources upserted by id, checks replaced by name."""
    resources: dict[str, dict] = {}
    checks: dict[str, dict] = {}
    for event in events:
        if event.type == "provision.resource":
            resources[event.data["id"]] = event.data
        else:
            checks[event.data["name"]] = event.data
    return list(resources.values()), list(checks.values())


def web_finished(events) -> bool:
    resources, checks = web_state(events)
    progress = resources[-1]["progress"] if resources else 0
    return progress == 100 and bool(checks) and all(c["state"] == "done" for c in checks)


async def test_ready_rollout_emits_events_the_web_finishes_on(db, store, setup, emitted):
    async def agent_reports():
        await asyncio.sleep(0.1)
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

    result, _ = await asyncio.gather(run(db, store, setup["request"], wait=2), agent_reports())
    assert (result["status"], result["code"]) == ("SUCCEEDED", "READY")
    events = provision(emitted)
    resources = [e.data for e in events if e.type == "provision.resource"]
    ids = [f"{JOB}:{part}" for part in ("namespace", "database", "application")]
    # Queued: every resource running at 0, before the Agent reports.
    assert [(r["id"], r["state"], r["progress"]) for r in resources[:3]] == [
        (i, "running", 0) for i in ids
    ]
    assert [r["key"] for r in resources[:3]] == ["cluster", "db", "app"]
    assert [r["name"] for r in resources[:3]] == [
        "Namespace cmp-prod-demo",
        "PostgreSQL demo-db",
        "App demo-app",
    ]
    assert [(r["id"], r["state"], r["progress"]) for r in resources[3:]] == [
        (ids[0], "done", 33.3),
        (ids[1], "done", 66.7),
        (ids[2], "done", 100.0),
    ]
    checks = [(e.data["name"], e.data["state"]) for e in events if e.type == "provision.check"]
    assert checks == [("pods_ready", "done"), ("db_connect", "done"), ("app_health", "done")]
    assert all(e.status == "running" for e in events)
    assert web_finished(events)
    assert not any(log["level"] == "warning" for log in emitted.logs)  # no rejected event
    assert [log["code"] for log in emitted.logs if log["level"] == "info"] == ["READY"]


async def test_failed_agent_check_marks_every_resource_failed_with_the_code(
    db, store, setup, emitted
):
    finish_job(db, store, setup, "FAILED", "DATABASE_NOT_READY")
    result = await run(db, store, setup["request"])
    assert (result["status"], result["code"]) == ("FAILED", "DATABASE_NOT_READY")
    events = provision(emitted)
    resources, checks = web_state(events)
    assert len(resources) == 3
    assert all(r["state"] == "failed" and r["detail"] == "DATABASE_NOT_READY" for r in resources)
    assert all(r["progress"] == 0 for r in resources)
    # pg_isready runs after the pod checks, so pods_ready passed and db_connect failed.
    assert [(c["name"], c["state"]) for c in checks] == [
        ("pods_ready", "done"),
        ("db_connect", "failed"),
    ]
    assert [e.status for e in events if e.data.get("state") == "failed"] == ["failed"] * 4
    assert not web_finished(events)
    assert [log["code"] for log in emitted.logs if log["level"] == "error"] == [
        "DATABASE_NOT_READY"
    ]


async def test_offline_agent_ends_failed_without_checks(db, store, setup, emitted):
    result = await run(db, store, setup["request"], wait=0.1)
    assert (result["status"], result["code"]) == ("FAILED", "AGENT_OFFLINE")
    resources, checks = web_state(provision(emitted))
    assert {(r["state"], r["detail"]) for r in resources} == {("failed", "AGENT_OFFLINE")}
    assert checks == []


async def test_rejected_dispatch_shows_one_failed_agent_job(db, store, setup, emitted):
    changed = {**setup["request"], "manifest_digest": "0" * 64}
    result = await run(db, store, changed)
    assert result["code"] == "APPROVAL_TARGET_MISMATCH"
    events = provision(emitted)
    assert [(e.data["id"], e.data["key"], e.data["state"], e.data["detail"]) for e in events] == [
        (f"{JOB}:rollout", "agent", "failed", "APPROVAL_TARGET_MISMATCH")
    ]


async def test_repeated_execution_of_the_same_revision_adds_no_events(db, store, setup, emitted):
    finish_job(db, store, setup, "SUCCEEDED", "READY")
    first = await run(db, store, setup["request"])
    count = len(provision(emitted))
    # A new Activity run (other attempt / activity id) for the same Revision reuses the job.
    second = await run(db, store, setup["request"])
    assert first["code"] == second["code"] == "READY"
    assert len(provision(emitted)) == count == 3 + 3 + 3
    assert web_finished(provision(emitted))
    with db() as session:
        assert session.query(AgentJob).count() == 1


def test_approved_network_policies_are_their_own_resource():
    from cmp_worker.provisioning_events import onprem_resources

    job = {
        "job_id": "rollout-1",
        "namespace": "ns",
        "network_policies": {"id": "np"},
        "database": {"release": "db"},
        "application": {"release": "app"},
    }
    assert [(r["id"], r["key"]) for r in onprem_resources(job)] == [
        ("rollout-1:namespace", "cluster"),
        ("rollout-1:network-policies", "network"),
        ("rollout-1:database", "db"),
        ("rollout-1:application", "app"),
    ]


async def test_events_carry_no_secret_values(db, store, setup, emitted):
    finish_job(db, store, setup, "SUCCEEDED", "READY")
    await run(db, store, setup["request"])
    with db() as session:
        payload = session.get(AgentJob, JOB).payload
    text = repr([e.data for e in provision(emitted)]) + repr(emitted.logs)
    # Secret names/keys and artifact digests are not put in the build events.
    for secret in (payload["database"]["password_secret"]["name"], "sha256", "password"):
        assert secret not in text
