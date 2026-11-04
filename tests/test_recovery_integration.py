"""Ticket 14 seam: real ProvisioningApprovalWorkflow under cancellation and Worker loss (F1~F8).

Provider Activities are fakes that block, heartbeat and count calls; the business DB is SQLite.
Expectations come from decisions F1~F5, not from the implementation.
"""

import asyncio

import pytest
from cmp_api.approval_delivery import TemporalApprovalDelivery
from cmp_api.approvals import PlanApproval, execution_status
from cmp_contracts.queues import (
    APPROVAL_WORKFLOW_NAME,
    PROVISIONING_QUEUE,
    WORKFLOW_QUEUE,
    approval_workflow_id,
)
from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker
from test_provisioning_result import GCP_WORKLOAD_REFS, Approved, stored


@pytest.fixture
def db(tmp_path):
    from cmp_api.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


class BlockingProvider:
    """execute_gcp_workloads that starts, heartbeats and blocks until cancelled."""

    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.outcome = {"status": "SUCCEEDED", "code": "WORKLOADS_READY", "resource_refs": {}}

    def activity(self):
        @activity.defn(name="execute_gcp_workloads")
        async def run(request: dict) -> dict:
            self.calls += 1
            self.started.set()
            while not self.release.is_set():
                activity.heartbeat("working")
                await asyncio.sleep(0.05)
            return self.outcome

        return run


def provisioning_worker(env, db, provider, **kwargs):
    approval = ApprovalActivities(db)
    return Worker(
        env.client,
        task_queue=PROVISIONING_QUEUE,
        activities=[
            approval.claim_approved_execution,
            approval.complete_approved_execution,
            provider.activity(),
        ],
        **kwargs,
    )


async def start(env, plan_id="plan-001") -> WorkflowHandle:
    return await env.client.start_workflow(
        APPROVAL_WORKFLOW_NAME,
        {"plan_id": plan_id, "revision": 1},
        id=approval_workflow_id(plan_id, 1),
        task_queue=WORKFLOW_QUEUE,
    )


async def deliver(env, approved):
    await TemporalApprovalDelivery(env.client).deliver(
        approval_id=approved.approval_id, plan_id=approved.plan_id, revision=1
    )


def marker(db, approved) -> str:
    with db() as session:
        return execution_status(session, session.get(PlanApproval, approved.approval_id))


async def handoff(db, consumer="DATA_MIGRATION"):
    return await ActivityEnvironment().run(
        ApprovalActivities(db).get_migration_handoff,
        {"plan_id": "plan-001", "revision": 1, "consumer": consumer},
    )


async def test_cancel_before_approval_ends_without_execution(db):
    approved = Approved(db, "GCP", "WORKLOADS")
    provider = BlockingProvider()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
        provisioning_worker(env, db, provider),
    ):
        handle = await start(env)
        await handle.cancel()
        outcome = await asyncio.wait_for(handle.result(), timeout=30)

    assert (outcome["status"], outcome["code"]) == ("CANCELLED", "CANCELLED_BEFORE_EXECUTION")
    assert provider.calls == 0
    assert marker(db, approved) == "NOT_STARTED"
    assert stored(db) is None


async def test_cancel_during_provider_step_records_unknown_not_deleted(db):
    approved = Approved(db, "GCP", "WORKLOADS")
    provider = BlockingProvider()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
        provisioning_worker(env, db, provider),
    ):
        handle = await start(env)
        await deliver(env, approved)
        await asyncio.wait_for(provider.started.wait(), timeout=30)
        await handle.cancel()
        outcome = await asyncio.wait_for(handle.result(), timeout=30)

    assert (outcome["status"], outcome["code"]) == ("UNKNOWN", "EXECUTION_CANCELLED")
    assert provider.calls == 1
    assert marker(db, approved) == "UNKNOWN"  # never left RUNNING, never "deleted"
    result = stored(db)
    assert (result.status, result.code) == ("UNKNOWN", "EXECUTION_CANCELLED")
    assert result.failure.recoverability == "CHECK_STATE_FIRST"
    assert "DELETED" not in result.model_dump_json()
    view = await handoff(db)
    assert view["proceed"] is False and "PROVISIONING_NOT_READY" in view["blocked_by"]


async def test_worker_loss_mid_step_is_unknown_and_never_re_executed(db):
    approved = Approved(db, "GCP", "WORKLOADS")
    provider = BlockingProvider()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
    ):
        handle = await start(env)
        first = provisioning_worker(env, db, provider)
        async with first:
            await deliver(env, approved)
            await asyncio.wait_for(provider.started.wait(), timeout=30)
            # Worker process dies: the running provider Activity is abandoned.
        assert marker(db, approved) == "RUNNING"

        async with provisioning_worker(env, db, provider):
            outcome = await asyncio.wait_for(handle.result(), timeout=60)

    assert (outcome["status"], outcome["code"]) == ("UNKNOWN", "PROVIDER_ACTIVITY_LOST")
    assert provider.calls == 1  # the restarted Worker did not run the provider again
    assert marker(db, approved) == "UNKNOWN"
    assert stored(db).failure.recoverability == "CHECK_STATE_FIRST"


async def test_redelivery_never_re_runs_a_lost_revision(db):
    approved = Approved(db, "GCP", "WORKLOADS")
    provider = BlockingProvider()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
    ):
        handle = await start(env)
        async with provisioning_worker(env, db, provider):
            await deliver(env, approved)
            await asyncio.wait_for(provider.started.wait(), timeout=30)
        async with provisioning_worker(env, db, provider):
            await asyncio.wait_for(handle.result(), timeout=60)
            # Re-delivering the same approval never re-runs the lost Revision (F1).
            await deliver(env, approved)
            await asyncio.sleep(0.2)
    assert provider.calls == 1
    assert marker(db, approved) == "UNKNOWN"


@pytest.mark.parametrize(
    ("step", "refs", "expected"),
    [
        (
            "execute_gcp_plan",
            {"gcp_project_id": "sbh26-prod", "state_resource_count": "5"},
            {"state_resource_count": "5"},
        ),
        (
            "execute_gcp_workloads",
            {
                **{
                    k: GCP_WORKLOAD_REFS[k]
                    for k in ("namespace", "storage_class", "db_secret_ref", "db_release")
                },
                "image_pull_secret": "cmp-ecr-pull",
            },
            {
                "namespace": "cmp-demo-target",
                "storage_class": "cmp-pd-balanced",
                "db_secret_ref": "cmp-demo-db-auth/password",
                "db_release": "cmp-demo-db",
                "image_pull_secret": "cmp-ecr-pull",
            },
        ),
    ],
)
def test_partial_resources_are_recorded_on_failure(db, step, refs, expected):
    from cmp_api.approvals import claim_execution, complete_execution

    approved = Approved(db, "GCP", "FULL")
    with db() as session:
        claim_execution(session, approved.approval_id)
        complete_execution(
            session,
            approved.approval_id,
            status="FAILED",
            code="TERRAFORM_APPLY_FAILED" if step.endswith("_plan") else "PVC_NOT_BOUND",
            step=step,
            resource_refs=refs,
        )
    result = stored(db)
    assert result.partial_resources == expected


def test_success_has_no_partial_resources(db):
    from cmp_api.approvals import claim_execution, complete_execution

    approved = Approved(db, "GCP", "WORKLOADS")
    with db() as session:
        claim_execution(session, approved.approval_id)
        complete_execution(
            session,
            approved.approval_id,
            status="SUCCEEDED",
            code="WORKLOADS_READY",
            step="execute_gcp_workloads",
            resource_refs=GCP_WORKLOAD_REFS,
        )
    assert stored(db).partial_resources == {}
