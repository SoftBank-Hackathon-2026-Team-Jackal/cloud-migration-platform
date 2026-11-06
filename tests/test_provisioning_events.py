"""Ticket 24: Provisioning emits approval.decision, plan.ready, provision.resource/check."""

import json

import pytest
from cmp_api.approvals import publish_plan_revision
from cmp_api.db import Base
from cmp_api.migration_events import InMemoryEmitter, list_events
from cmp_providers.aws_apply import CommandResult
from cmp_providers.terraform_events import ApplyProgress, resource_key
from cmp_worker.activities.aws_infra import AwsInfraActivities
from cmp_worker.events import configure_emitter
from cmp_worker.provisioning_events import PlanReadyEvents
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment
from test_aws_apply import FakeRunner, artifacts
from test_aws_execution_activity import claim
from test_aws_workloads import FakeCluster, FakeRuntime, Plan, activity_for, run


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def runtime(cluster):
    return FakeRuntime(cluster)


@pytest.fixture
def emitted():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


def tf(kind: str, addr: str, rtype: str, **extra) -> bytes:
    key = "change" if kind == "planned_change" else "hook"
    body = {"resource": {"addr": addr, "resource_type": rtype}, "action": "create", **extra}
    return json.dumps({"type": kind, "@message": f"{addr}: secret-ish text", key: body}).encode()


class JsonApplyRunner(FakeRunner):
    def __init__(self, lines: list[bytes], **kwargs):
        super().__init__(**kwargs)
        self.lines = lines

    def __call__(self, args, cwd, env, timeout):
        result = super().__call__(args, cwd, env, timeout)
        if args[1:2] == ["apply"]:
            assert "-json" in args
            return CommandResult(result.returncode, b"\n".join(self.lines), result.stderr)
        return result


APPLY = [
    tf("planned_change", "aws_vpc.target", "aws_vpc"),
    tf("planned_change", "aws_eks_cluster.this", "aws_eks_cluster"),
    tf("apply_start", "aws_vpc.target", "aws_vpc"),
    tf("apply_complete", "aws_vpc.target", "aws_vpc", id_key="id", id_value="vpc-123"),
    tf("apply_start", "aws_eks_cluster.this", "aws_eks_cluster"),
    tf("apply_complete", "aws_eks_cluster.this", "aws_eks_cluster"),
    b"not json",
]


async def test_terraform_apply_json_becomes_resource_events(db, tmp_path, emitted):
    manifest, preview, _ = artifacts(tmp_path)
    with db() as s:
        publish_plan_revision(s, manifest, preview)
    activities = AwsInfraActivities(
        db, artifact_root=tmp_path, aws_profile="cmp-aws-prod", runner=JsonApplyRunner(APPLY)
    )
    result = await ActivityEnvironment().run(activities.execute_aws_plan, claim(manifest))
    assert result["status"] == "SUCCEEDED"
    events = emitted.of_type("provision.resource")
    assert [(e.data["id"], e.data["state"], e.progress) for e in events] == [
        ("aws_vpc.target", "running", 0.0),
        ("aws_vpc.target", "done", 50.0),
        ("aws_eks_cluster.this", "running", 50.0),
        ("aws_eks_cluster.this", "done", 100.0),
    ]
    assert {e.data["key"] for e in events} == {"network", "cluster"}
    assert all(
        e.migration_id == manifest.migration_id and e.phase == "provisioning" for e in events
    )
    assert "vpc-123" not in str([e.data for e in events])  # IDs and @message never copied
    assert emitted.logs[-1]["code"] == result["code"]


async def test_terraform_errored_resource_and_stale_diagnostic(db, tmp_path, emitted):
    manifest, preview, _ = artifacts(tmp_path)
    with db() as s:
        publish_plan_revision(s, manifest, preview)
    stale = json.dumps(
        {
            "type": "diagnostic",
            "diagnostic": {"severity": "error", "summary": "Saved plan is stale"},
        }
    ).encode()
    lines = [
        tf("apply_start", "aws_vpc.target", "aws_vpc"),
        tf("apply_errored", "aws_vpc.target", "aws_vpc"),
        stale,
    ]
    runner = JsonApplyRunner(lines, apply_rc=1)
    activities = AwsInfraActivities(
        db, artifact_root=tmp_path, aws_profile="cmp-aws-prod", runner=runner
    )
    result = await ActivityEnvironment().run(activities.execute_aws_plan, claim(manifest))
    assert result["code"] == "SAVED_PLAN_STALE"  # read from the JSON diagnostic, not stderr
    failed = emitted.of_type("provision.resource")[-1]
    assert failed.data["state"] == "failed" and failed.status == "failed"
    assert emitted.logs[-1]["level"] == "error"


def test_progress_uses_preview_total_without_planned_changes():
    seen = []
    progress = ApplyProgress(lambda data, p: seen.append(p), expected_total=4)
    progress.feed(tf("apply_complete", "google_compute_network.vpc", "google_compute_network"))
    assert seen == [25.0]
    assert resource_key("google_container_node_pool") == "cluster"
    assert resource_key("aws_iam_role") == "iam"
    assert resource_key("random_id") == "other"


async def test_workloads_emit_helm_resources_and_checks(tmp_path, db, cluster, runtime, emitted):
    plan = Plan(tmp_path / "artifacts", db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert result["status"] == "SUCCEEDED"
    resources = [(e.data["key"], e.data["state"]) for e in emitted.of_type("provision.resource")]
    assert resources == [
        ("db", "running"),
        ("db", "done"),
        ("app", "running"),
        ("app", "done"),
        ("lb", "running"),
        ("lb", "done"),
    ]
    checks = [(e.data["name"], e.data["state"]) for e in emitted.of_type("provision.check")]
    assert checks == [
        ("pods_ready", "running"),
        ("pods_ready", "done"),
        ("db_connect", "running"),
        ("db_connect", "done"),
        ("pods_ready", "running"),
        ("pods_ready", "done"),
        ("app_health", "running"),
        ("app_health", "done"),
    ]
    assert emitted.events[-1].progress == 100.0
    assert "password" not in json.dumps([e.data for e in emitted.events])


async def test_failed_check_is_reported_as_failed(tmp_path, db, cluster, runtime, emitted):
    cluster.db_probe = None
    plan = Plan(tmp_path / "artifacts", db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert result["code"] == "DB_CONNECT_FAILED"
    last = emitted.of_type("provision.check")[-1]
    assert (last.data, last.status) == ({"name": "db_connect", "state": "failed"}, "failed")
    assert emitted.logs[-1]["code"] == "DB_CONNECT_FAILED"


class FakePublisher:
    def publish(self, migration_id):
        return {
            "status": "PUBLISHED",
            "code": "PLAN_PUBLISHED",
            "plan_id": "plan-1",
            "revision": 2,
            "provider": "AWS",
            "scope": "INFRASTRUCTURE",
            "manifest_digest": "a" * 64,
            "preview_digest": "b" * 64,
        }

    def publish_failed(self, migration_id):
        return {"status": "FAILED", "code": "PREVIEW_ERRORS", "issues": []}


async def test_plan_ready_after_publication(emitted):
    wrapped = PlanReadyEvents(FakePublisher())

    async def body():
        wrapped.publish("migration-9")
        wrapped.publish_failed("migration-9")

    from temporalio import activity

    @activity.defn(name="probe")
    async def probe() -> None:
        await body()

    await ActivityEnvironment().run(probe)
    [ready] = emitted.of_type("plan.ready")
    assert ready.migration_id == "migration-9" and ready.status == "done"
    assert ready.data == {
        "plan_id": "plan-1",
        "revision": 2,
        "manifest_digest": "a" * 64,
        "preview_digest": "b" * 64,
        "scope": "INFRASTRUCTURE",
    }


def test_approval_decision_event_from_api(tmp_path):
    from cmp_api.config import settings
    from cmp_api.main import app
    from cmp_api.routes.approvals import get_approval_delivery, get_session
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from test_approval_gate import ADMIN_KEY, BASE, FakeDelivery, body, manifest, preview

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    delivery = FakeDelivery()
    settings_key, settings.approval_admin_key = settings.approval_admin_key, ADMIN_KEY

    def session():
        with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_approval_delivery] = lambda: delivery
    try:
        client = TestClient(app)
        plan, display = manifest(), None
        display = preview(plan)
        with sessions() as s:
            publish_plan_revision(s, plan, display)
        cred = client.post(
            "/provisioning/approvers",
            json={"approver_id": "alice", "migration_ids": ["migration-001"]},
            headers={"X-Approval-Admin-Key": ADMIN_KEY},
        ).json()["credential"]
        headers = {"Authorization": f"Bearer {cred}"}
        delivery.fail = True
        first = client.post(f"{BASE}/1/approvals", json=body(plan, display), headers=headers).json()
        delivery.fail = False
        client.post(f"{BASE}/1/approvals/{first['approval_id']}/delivery", headers=headers)
        with sessions() as s:
            events = list_events(s, plan.migration_id)
    finally:
        app.dependency_overrides.clear()
        settings.approval_admin_key = settings_key
        engine.dispose()
    assert [(e.type, e.status, e.data["delivery_status"]) for e in events] == [
        ("approval.decision", "waiting", "FAILED"),
        ("approval.decision", "done", "DELIVERED"),
    ]
    assert "approver_id" not in events[0].data
    assert events[1].data["delivery_attempts"] == 2 and events[1].phase == "awaiting_approval"
