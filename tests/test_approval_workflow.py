"""Ticket 07 seam: real ProvisioningApprovalWorkflow + DB re-check Activity + fake executor."""

import asyncio
from pathlib import Path

import pytest
from cmp_api.approval_delivery import TemporalApprovalDelivery
from cmp_api.approvals import (
    PlanRevision,
    authenticate_approver,
    execution_status,
    publish_plan_revision,
    record_decision,
    register_approver,
)
from cmp_api.db import Base
from cmp_contracts.plan import PlanManifest, Preview
from cmp_contracts.queues import (
    APPROVAL_WORKFLOW_NAME,
    PROVISIONING_QUEUE,
    WORKFLOW_QUEUE,
    approval_workflow_id,
)
from cmp_providers.artifacts import sha256_json
from cmp_providers.plans import manifest_digest
from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

FIXTURES = Path(__file__).parent / "fixtures/contracts"


def manifest(revision=1):
    data = PlanManifest.model_validate_json((FIXTURES / "aws-plan.json").read_text()).model_dump(
        mode="json"
    )
    data["plan"]["revision"] = revision
    return PlanManifest.model_validate(data)


def preview(plan):
    return Preview(
        contract_version="1",
        manifest_digest=manifest_digest(plan),
        changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "demo"}],
    )


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def executor():
    return FakeExecutor()


@pytest.fixture
async def temporal(db, executor):
    activities = ApprovalActivities(db, executor)
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
        Worker(
            env.client,
            task_queue=PROVISIONING_QUEUE,
            activities=[activities.execute_approved_plan],
        ),
    ):
        yield env.client


def decide(db, decision="APPROVE", revision=1):
    plan = manifest(revision)
    display = preview(plan)
    with db() as s:
        publish_plan_revision(s, plan, display)
        credential = register_approver(s, approver_id="alice", migration_ids=["migration-001"])
        record, _ = record_decision(
            s,
            approver=authenticate_approver(s, credential),
            plan_id="plan-001",
            revision=revision,
            decision=decision,
            scope=plan.scope,
            manifest_digest=manifest_digest(plan),
            preview_digest=sha256_json(display.model_dump(mode="json")),
        )
        return record.id


async def result(client, revision=1):
    handle = client.get_workflow_handle(approval_workflow_id("plan-001", revision))
    return await asyncio.wait_for(handle.result(), timeout=30)


async def test_workflow_waits_for_approval_and_never_executes_before_it(temporal, db, executor):
    handle = await temporal.start_workflow(
        APPROVAL_WORKFLOW_NAME,
        {"plan_id": "plan-001", "revision": 1},
        id=approval_workflow_id("plan-001", 1),
        task_queue=WORKFLOW_QUEUE,
    )
    assert (await handle.query("status"))["state"] == "AWAITING_APPROVAL"
    assert executor.calls == []

    approval_id = decide(db)
    await TemporalApprovalDelivery(temporal).deliver(
        approval_id=approval_id, plan_id="plan-001", revision=1
    )
    outcome = await result(temporal)
    assert outcome["status"] == "EXECUTED"
    assert [call["approval_id"] for call in executor.calls] == [approval_id]
    assert (await handle.query("status"))["state"] == "EXECUTED"


async def test_duplicate_and_late_deliveries_execute_exactly_once(temporal, db, executor):
    approval_id = decide(db)
    delivery = TemporalApprovalDelivery(temporal)
    for _ in range(3):
        await delivery.deliver(approval_id=approval_id, plan_id="plan-001", revision=1)
    assert (await result(temporal))["status"] == "EXECUTED"
    await delivery.deliver(approval_id=approval_id, plan_id="plan-001", revision=1)
    assert len(executor.calls) == 1
    with db() as s:
        from cmp_api.approvals import PlanApproval

        assert execution_status(s, s.get(PlanApproval, approval_id)) == "SUCCEEDED"
    assert await delivery.workflow_state(plan_id="plan-001", revision=1) == "EXECUTED"


async def test_rejected_plan_never_reaches_executor(temporal, db, executor):
    approval_id = decide(db, "REJECT")
    await TemporalApprovalDelivery(temporal).deliver(
        approval_id=approval_id, plan_id="plan-001", revision=1
    )
    outcome = await result(temporal)
    assert outcome["status"] == "REJECTED"
    assert executor.calls == []


async def test_revision_superseded_after_approval_is_blocked_at_execution(temporal, db, executor):
    approval_id = decide(db)
    newer = manifest(2)
    with db() as s:
        publish_plan_revision(s, newer, preview(newer))
    await TemporalApprovalDelivery(temporal).deliver(
        approval_id=approval_id, plan_id="plan-001", revision=1
    )
    outcome = await result(temporal)
    assert (outcome["status"], outcome["code"]) == ("BLOCKED", "STALE_REVISION")
    assert executor.calls == []


async def test_plan_changed_after_approval_is_blocked_by_digest_recheck(temporal, db, executor):
    approval_id = decide(db)
    with db() as s:
        row = s.get(PlanRevision, ("plan-001", 1))
        changed = dict(row.manifest)
        changed["target"] = {**changed["target"], "account_id": "another-account"}
        row.manifest = changed
        s.commit()
    await TemporalApprovalDelivery(temporal).deliver(
        approval_id=approval_id, plan_id="plan-001", revision=1
    )
    outcome = await result(temporal)
    assert outcome["status"] == "BLOCKED"
    assert outcome["code"] in {"APPROVAL_TARGET_MISMATCH", "STALE_PREVIEW"}
    assert executor.calls == []


async def test_signal_without_db_approval_is_blocked(temporal, db, executor):
    plan = manifest()
    with db() as s:
        publish_plan_revision(s, plan, preview(plan))
    await TemporalApprovalDelivery(temporal).deliver(
        approval_id="00000000-0000-0000-0000-000000000000", plan_id="plan-001", revision=1
    )
    outcome = await result(temporal)
    assert (outcome["status"], outcome["code"]) == ("BLOCKED", "APPROVAL_NOT_FOUND")
    assert executor.calls == []


async def test_recorded_but_undelivered_approval_does_not_execute_until_redelivered(
    temporal, db, executor
):
    approval_id = decide(db)
    delivery = TemporalApprovalDelivery(temporal)
    assert await delivery.workflow_state(plan_id="plan-001", revision=1) == "NOT_STARTED"
    assert executor.calls == []
    await delivery.deliver(approval_id=approval_id, plan_id="plan-001", revision=1)
    assert (await result(temporal))["status"] == "EXECUTED"
    assert len(executor.calls) == 1


def test_alembic_migration_matches_approval_models(tmp_path, monkeypatch):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from cmp_api.config import settings

    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
    assert [d for d in diff if "provisioning" in str(d)] == []
    command.downgrade(Config("alembic.ini"), "20261103_01")
