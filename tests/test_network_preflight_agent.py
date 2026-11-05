"""MNP-01 N3: Network Preflight through the on-premises Agent job queue.

Seams: the platform job service (`submit_preflight`/`poll_job`/`report_job`), the HTTP Agent
routes, the Agent's `RolloutAgent` + `PreflightJobRunner` with a fake probe executor, and the
`run_network_preflight` Activity with the real business-DB transport. Real kubectl effects are
covered by `scripts/smoke_onprem_preflight.py` on an isolated k3d cluster.
"""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from cmp_api.agent_preflight import (
    PreflightJobError,
    preflight_state,
    submit_preflight,
)
from cmp_api.agent_registry import (
    AgentJob,
    AgentRegistrationError,
    RegisteredAgent,
    create_enrollment,
    poll_job,
    redeem_enrollment,
    report_job,
    revoke_agent,
)
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.migration_events import InMemoryEmitter
from cmp_api.routes.agents import get_session as agent_session
from cmp_contracts.events import validate_data
from cmp_contracts.network_preflight import (
    NetworkPreflightJob,
    NetworkPreflightResult,
    preflight_job_id,
)
from cmp_providers.agent_preflight import PreflightJobRunner
from cmp_providers.network_preflight import (
    ProbeLocationUnavailable,
    input_fingerprint,
    may_start_data_migration,
)
from cmp_providers.onprem_agent import ClusterUnavailable, RolloutAgent
from cmp_worker.activities.network_preflight import (
    DbAgentPreflightTransport,
    NetworkPreflightActivities,
    choose_executor,
)
from cmp_worker.events import configure_emitter
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment
from test_network_preflight import SECRET_VALUE, FakeExecutor, good_observation, request

AGENT, CLUSTER = "agent-001", "k3s-uid-001"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def register(session, agent_id=AGENT, cluster_uid=CLUSTER, owner="owner-a") -> str:
    token = create_enrollment(session, owner_id=owner, agent_id=agent_id, cluster_uid=cluster_uid)
    return redeem_enrollment(session, token=token, agent_id=agent_id, cluster_uid=cluster_uid)


@pytest.fixture
def credential(db):
    with db() as session:
        return register(session)


@pytest.fixture
def emitter():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


def submit(db, req=None, **kwargs) -> str:
    req = req or request()
    with db() as session:
        return submit_preflight(session, req, input_fingerprint(req), **kwargs)


def observation_json(**changes) -> dict:
    return good_observation(**changes).model_dump(mode="json")


# --- contract ---------------------------------------------------------------------------------


def test_job_is_bound_to_probe_target_and_id_to_migration_and_fingerprint():
    req = request()
    fp = input_fingerprint(req)
    job_id = preflight_job_id(req.migration_id, fp)
    assert job_id == preflight_job_id(req.migration_id, fp)
    assert job_id != preflight_job_id(
        req.migration_id, input_fingerprint(request(source_db_port=5433))
    )
    assert job_id != preflight_job_id("migration-002", fp)
    with pytest.raises(ValueError):
        NetworkPreflightJob(
            mode="NETWORK_PREFLIGHT", job_id=job_id, agent_id="agent-002", cluster_uid=CLUSTER,
            request=req,
        )  # fmt: skip


# --- platform job service ---------------------------------------------------------------------


def test_submit_queues_one_reference_only_job_and_dedupes(db, credential):
    first = submit(db)
    assert submit(db) == first  # duplicate request reuses the queued job
    with db() as session:
        rows = session.scalars(select(AgentJob)).all()
        assert [(r.id, r.status, r.agent_id, r.owner_id) for r in rows] == [
            (first, "QUEUED", AGENT, "owner-a")
        ]
        payload = rows[0].payload
    assert payload["mode"] == "NETWORK_PREFLIGHT"
    assert payload["request"]["db_auth_ref"] == {
        "secret_name": "source-db-auth", "username_key": "user", "password_key": "password",
    }  # fmt: skip
    assert SECRET_VALUE not in json.dumps(payload)


def test_submit_refuses_unknown_revoked_or_mismatched_agent(db, credential):
    with pytest.raises(PreflightJobError, match="AGENT_TARGET_MISMATCH"):
        submit(
            db, request(probe_target={"agent_id": AGENT, "cluster_uid": "other", "namespace": "ns"})
        )
    with pytest.raises(PreflightJobError, match="AGENT_NOT_REGISTERED"):
        submit(
            db,
            request(
                probe_target={"agent_id": "agent-x", "cluster_uid": CLUSTER, "namespace": "ns"}
            ),
        )
    with db() as session:
        revoke_agent(session, agent_id=AGENT)
    with pytest.raises(PreflightJobError, match="AGENT_REVOKED"):
        submit(db)


def test_only_the_target_agent_can_claim_or_report(db, credential):
    with db() as session:
        other = register(session, agent_id="agent-002", cluster_uid=CLUSTER)
    job_id = submit(db)
    with db() as session:
        assert poll_job(session, agent_id="agent-002", credential=other) is None
        with pytest.raises(AgentRegistrationError, match="JOB_NOT_FOUND"):
            report_job(
                session, agent_id="agent-002", credential=other, job_id=job_id,
                status="SUCCEEDED", result={"code": "PROBED", "observation": observation_json()},
            )  # fmt: skip
        claimed = poll_job(session, agent_id=AGENT, credential=credential)
    assert claimed["job_id"] == job_id and claimed["payload"]["agent_id"] == AGENT


def test_claim_refuses_job_whose_cluster_no_longer_matches_the_agent(db, credential):
    job_id = submit(db)
    with db() as session:
        session.get(RegisteredAgent, AGENT).cluster_uid = "re-registered-elsewhere"
        session.commit()
        assert poll_job(session, agent_id=AGENT, credential=credential) is None
        job = session.get(AgentJob, job_id)
        assert (job.status, job.result) == ("FAILED", {"code": "AGENT_TARGET_MISMATCH"})


def test_lifecycle_report_refetch_and_requeue_after_reuse_window(db, credential):
    job_id = submit(db)
    now = datetime.now(UTC)
    with db() as session:
        poll_job(session, agent_id=AGENT, credential=credential)
        result = {"code": "PROBED", "observation": observation_json()}
        report_job(session, agent_id=AGENT, credential=credential, job_id=job_id,
                   status="SUCCEEDED", result=result)  # fmt: skip
        # A repeated report after a lost acknowledgement is accepted, not a conflict.
        report_job(session, agent_id=AGENT, credential=credential, job_id=job_id,
                   status="SUCCEEDED", result=result)  # fmt: skip
        status, code, obs = preflight_state(session, job_id)
    assert (status, code) == ("SUCCEEDED", "PROBED") and obs == observation_json()
    # Lost Activity response: the same request inside the window gets the stored observation.
    assert submit(db, now=now + timedelta(seconds=60)) == job_id
    with db() as session:
        assert session.get(AgentJob, job_id).status == "SUCCEEDED"
    # Later the same request is probed again (e.g. the Secret value was fixed).
    assert submit(db, now=now + timedelta(seconds=600)) == job_id
    with db() as session:
        job = session.get(AgentJob, job_id)
        assert (job.status, job.result) == ("QUEUED", None)


@pytest.mark.parametrize(
    ("status", "result"),
    [
        ("SUCCEEDED", {"code": "PROBED"}),
        ("SUCCEEDED", {"code": "READY", "observation": observation_json()}),
        ("FAILED", {"code": "CLUSTER_MISMATCH", "observation": observation_json()}),
        ("SUCCEEDED", {"code": "PROBED", "observation": {"node": "n", "password": "x"}}),
        ("SUCCEEDED", {"code": "PROBED", "observation": observation_json(mtu={"x": "y" * 5000})}),
    ],
)
def test_report_shape_is_enforced(db, credential, status, result):
    job_id = submit(db)
    with db() as session:
        poll_job(session, agent_id=AGENT, credential=credential)
        with pytest.raises(AgentRegistrationError, match="INVALID_JOB_RESULT"):
            report_job(session, agent_id=AGENT, credential=credential, job_id=job_id,
                       status=status, result=result)  # fmt: skip


def test_older_agent_code_only_rejection_is_accepted(db, credential):
    job_id = submit(db)
    with db() as session:
        poll_job(session, agent_id=AGENT, credential=credential)
        report_job(session, agent_id=AGENT, credential=credential, job_id=job_id,
                   status="FAILED", result={"code": "UNSUPPORTED_JOB"})  # fmt: skip
        assert preflight_state(session, job_id)[:2] == ("FAILED", "UNSUPPORTED_JOB")


def test_http_claim_and_report_with_observation(db, credential):
    job_id = submit(db)

    def session_override():
        with db() as session:
            yield session

    app.dependency_overrides[agent_session] = session_override
    try:
        client = TestClient(app)
        auth = {"Authorization": f"Bearer {credential}"}
        claim = client.post(f"/agents/{AGENT}/jobs/claim", headers=auth)
        assert claim.status_code == 200, claim.text
        job = claim.json()["job"]
        assert job["job_id"] == job_id and job["payload"]["mode"] == "NETWORK_PREFLIGHT"
        body = {"status": "SUCCEEDED", "code": "PROBED", "observation": observation_json()}
        sent = client.post(f"/agents/{AGENT}/jobs/{job_id}/result", headers=auth, json=body)
        assert sent.status_code == 202, sent.text
        bad = client.post(
            f"/agents/{AGENT}/jobs/{job_id}/result",
            headers=auth,
            json={**body, "observation": {**body["observation"], "secret": SECRET_VALUE}},
        )
        assert bad.status_code == 422  # unknown observation fields are rejected, never stored
    finally:
        app.dependency_overrides.clear()
    with db() as session:
        assert preflight_state(session, job_id)[0] == "SUCCEEDED"


# --- Agent side ------------------------------------------------------------------------------


class FakeTarget:
    def __init__(self, cluster_uid=CLUSTER, namespace=None, error=None):
        self.cluster_uid = cluster_uid
        self.namespace = namespace if namespace is not None else {
            "metadata": {"name": "cmp-migration", "annotations": {"cmp.dev/environment-id": "env-1"}}
        }  # fmt: skip
        self.error = error

    def kubectl(self, *args, content=None):
        raise AssertionError("no kubectl in this test")

    def get(self, kind, name, namespace=None):
        assert kind == "namespace" and name == "cmp-migration"
        if self.error:
            raise self.error
        return self.namespace or None


class FakeClient:
    """Agent HTTP client double backed by the real job service."""

    def __init__(self, db):
        self.db, self.reports = db, []

    def poll_once(self, *, agent_id, credential):
        with self.db() as session:
            job = poll_job(session, agent_id=agent_id, credential=credential)
        return json.loads(json.dumps(job)) if job else None

    def report(self, *, agent_id, credential, job_id, status, code, observation=None):
        self.reports.append((job_id, status, code, observation))
        result = {"code": code} | ({"observation": observation} if observation else {})
        with self.db() as session:
            report_job(session, agent_id=agent_id, credential=credential, job_id=job_id,
                       status=status, result=result)  # fmt: skip


def agent(db, credential, target=None, executor=None, preflight=True):
    runner = PreflightJobRunner(target or FakeTarget(), (lambda: executor or FakeExecutor()))
    client = FakeClient(db)
    return RolloutAgent(AGENT, credential, client, target or FakeTarget(),
                        runner if preflight else None), client  # fmt: skip


def test_agent_probes_with_executor_and_reports_observation_only(db, credential):
    job_id = submit(db)
    executor = FakeExecutor()
    rollout_agent, client = agent(db, credential, executor=executor)
    outcome = rollout_agent.poll_once()
    assert outcome == {"job_id": job_id, "status": "SUCCEEDED", "code": "PROBED"}
    assert [r.migration_id for r in executor.calls] == ["migration-001"]
    ((_, _, _, observation),) = client.reports
    assert observation == observation_json()
    assert SECRET_VALUE not in json.dumps(client.reports)


@pytest.mark.parametrize(
    ("target", "executor", "preflight", "expected"),
    [
        (FakeTarget(cluster_uid="other"), None, True, ("FAILED", "CLUSTER_MISMATCH")),
        (FakeTarget(namespace={}), None, True, ("FAILED", "NAMESPACE_NOT_OWNED")),
        (FakeTarget(namespace={"metadata": {"annotations": {}}}), None, True,
         ("FAILED", "NAMESPACE_NOT_OWNED")),
        (FakeTarget(error=ClusterUnavailable()), None, True,
         ("UNKNOWN", "PROBE_LOCATION_UNAVAILABLE")),
        (None, FakeExecutor(error=ProbeLocationUnavailable()), True,
         ("UNKNOWN", "PROBE_LOCATION_UNAVAILABLE")),
        (None, None, False, ("FAILED", "PREFLIGHT_NOT_CONFIGURED")),
    ],
)  # fmt: skip
def test_agent_refuses_before_creating_any_pod(
    db, credential, target, executor, preflight, expected
):
    submit(db)
    executor = executor or FakeExecutor()
    rollout_agent, _ = agent(db, credential, target=target, executor=executor, preflight=preflight)
    outcome = rollout_agent.poll_once()
    assert (outcome["status"], outcome["code"]) == expected
    if expected[1] != "PROBE_LOCATION_UNAVAILABLE" or target is not None:
        assert executor.calls == []


def test_agent_runtime_wires_probe_image_from_its_own_configuration():
    from cmp_providers.agent_preflight import probe_executor_factory

    assert probe_executor_factory(FakeTarget(), "", frozenset()) is None
    mutable = probe_executor_factory(FakeTarget(), "postgres:16", frozenset({"postgres:16"}))
    with pytest.raises(ValueError):
        mutable()  # not digest-pinned: the runner reports PREFLIGHT_NOT_CONFIGURED
    pinned = "postgres@sha256:" + "a" * 64
    assert probe_executor_factory(FakeTarget(), pinned, frozenset({pinned}))() is not None


# --- Worker Activity -------------------------------------------------------------------------


def activity_with(db, **agent_options):
    transport = DbAgentPreflightTransport(db)
    direct = FakeExecutor(good_observation(node="direct-node"))
    options = {"poll_seconds": 0.02, "wait_seconds": 1.0} | agent_options
    return NetworkPreflightActivities(
        lambda req: choose_executor(req, transport, lambda: direct, **options)
    ), direct


def run_activity(activities, payload):
    return ActivityEnvironment().run(activities.run_network_preflight, payload)


async def test_activity_uses_agent_job_and_emits_data_preflight(db, credential, emitter):
    activities, direct = activity_with(db)
    rollout_agent, _ = agent(db, credential)
    payload = request().model_dump(mode="json")
    stop = threading.Event()

    def agent_loop():
        while not stop.is_set() and rollout_agent.poll_once() is None:
            stop.wait(0.02)

    worker = threading.Thread(target=agent_loop)
    worker.start()
    try:
        result = await run_activity(activities, payload)
    finally:
        stop.set()
        worker.join()
    assert result["status"] == "PASS" and direct.calls == []
    parsed = NetworkPreflightResult.model_validate_json(json.dumps(result))
    assert may_start_data_migration(parsed, request()) == (True, [])
    (event,) = emitter.events
    assert (event.type, event.phase, event.status) == ("data.preflight", "data_copy", "done")
    assert validate_data("data.preflight", event.data) == event.data
    assert event.data["may_start"] is True
    assert {c["name"] for c in event.data["checks"]} == {
        "cidr_overlap", "route", "db_path", "large_transfer",
    }  # fmt: skip
    dumped = json.dumps([result, event.data, emitter.logs])
    assert SECRET_VALUE not in dumped and "source-db-auth" not in json.dumps(event.data)


async def test_activity_reuses_stored_observation_after_lost_response(db, credential, emitter):
    job_id = submit(db)
    with db() as session:
        poll_job(session, agent_id=AGENT, credential=credential)
        report_job(session, agent_id=AGENT, credential=credential, job_id=job_id,
                   status="SUCCEEDED", result={"code": "PROBED", "observation": observation_json()})  # fmt: skip
    activities, _ = activity_with(db)
    result = await run_activity(activities, request().model_dump(mode="json"))
    assert result["status"] == "PASS"  # no Agent poll needed: the stored observation is reused
    with db() as session:
        assert len(session.scalars(select(AgentJob)).all()) == 1


async def test_offline_agent_times_out_to_unknown_and_keeps_job(db, credential, emitter):
    activities, _ = activity_with(db, wait_seconds=0.1)
    result = await run_activity(activities, request().model_dump(mode="json"))
    assert result["status"] == "UNKNOWN"
    assert {c["code"] for c in result["checks"][1:]} == {"AGENT_OFFLINE"}
    with db() as session:
        assert [j.status for j in session.scalars(select(AgentJob))] == ["QUEUED"]
    (event,) = emitter.events
    assert event.status == "failed" and event.data["may_start"] is False


async def test_running_job_without_result_is_result_pending(db, credential, emitter):
    submit(db)
    with db() as session:
        poll_job(session, agent_id=AGENT, credential=credential)  # Agent claimed, then vanished
    activities, _ = activity_with(db, wait_seconds=0.1)
    result = await run_activity(activities, request().model_dump(mode="json"))
    assert {c["code"] for c in result["checks"][1:]} == {"AGENT_RESULT_PENDING"}


@pytest.mark.parametrize(
    ("code", "expected"),
    [("UNSUPPORTED_JOB", "AGENT_JOB_UNSUPPORTED"), ("NAMESPACE_NOT_OWNED", "NAMESPACE_NOT_OWNED")],
)
async def test_agent_rejection_maps_to_unknown_with_clear_code(
    db, credential, emitter, code, expected
):
    activities, _ = activity_with(db)

    def old_agent():
        # Agent v0.2.0 validates every job as PRODUCTION_ROLLOUT and reports UNSUPPORTED_JOB.
        for _ in range(200):
            with db() as session:
                job = poll_job(session, agent_id=AGENT, credential=credential)
                if job:
                    report_job(session, agent_id=AGENT, credential=credential,
                               job_id=job["job_id"], status="FAILED", result={"code": code})  # fmt: skip
                    return
            threading.Event().wait(0.01)

    worker = threading.Thread(target=old_agent)
    worker.start()
    try:
        result = await run_activity(activities, request().model_dump(mode="json"))
    finally:
        worker.join()
    assert result["status"] == "UNKNOWN"
    assert {c["code"] for c in result["checks"][1:]} == {expected}


async def test_unregistered_agent_keeps_direct_kubeconfig_path(db, emitter):
    activities, direct = activity_with(db)
    result = await run_activity(activities, request().model_dump(mode="json"))
    assert result["status"] == "PASS" and len(direct.calls) == 1
    assert result["probe_location"]["node"] == "direct-node"


async def test_cidr_failure_never_queues_an_agent_job(db, credential, emitter):
    activities, _ = activity_with(db)
    bad = request(tunnel_cidrs=["10.20.0.0/24"]).model_dump(mode="json")
    result = await run_activity(activities, bad)
    assert result["status"] == "FAIL"
    with db() as session:
        assert session.scalars(select(AgentJob)).all() == []


async def test_registry_failure_is_unknown_not_a_silent_location_switch(emitter):
    class Broken:
        def is_agent(self, agent_id):
            raise RuntimeError("db down")

    direct = FakeExecutor()
    activities = NetworkPreflightActivities(
        lambda req: choose_executor(req, Broken(), lambda: direct)
    )
    result = await run_activity(activities, request().model_dump(mode="json"))
    assert {c["code"] for c in result["checks"][1:]} == {"AGENT_REGISTRY_UNAVAILABLE"}
    assert direct.calls == []
