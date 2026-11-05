"""Ticket 49: failure events, preserved failure codes, and no `running` for closed Workflows.

Seams: Temporal time-skipping server with the real MigrationWorkflow/approval Workflow, fake
Provisioning Activities plus the real `emit_migration_event` (InMemoryEmitter); the API state
reader against the same server; SQLite for the execution marker.
"""

import asyncio
import os

import pytest
from cmp_api import migration_state
from cmp_api.approvals import (
    EXECUTION_STEP,
    PlanApproval,
    PlanExecution,
    claim_execution,
    execution_status,
)
from cmp_api.db import Base
from cmp_api.event_stream import migration_status, snapshot
from cmp_api.migration_events import InMemoryEmitter
from cmp_api.migration_state import read_migration_state
from cmp_contracts.queues import (
    APPROVAL_SIGNAL,
    APPROVAL_WORKFLOW_NAME,
    PROVISIONING_QUEUE,
    WORKFLOW_QUEUE,
)
from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.events import configure_emitter, emit_migration_event
from cmp_worker.workers.analysis import create_worker as analysis_worker
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
from cmp_worker.workers.sandbox import create_worker as sandbox_worker
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_approval_workflow import FakeProvider, decide

LOCK_FAILURE = {"status": "FAILED", "code": "PLAN_FAILED", "issues": ["MISSING_PROVIDER_LOCK"]}


@pytest.fixture
def emitter():
    stored = InMemoryEmitter()
    previous = configure_emitter(stored)
    yield stored
    configure_emitter(previous)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def provisioning_worker(client, publish):
    """`publish`: a result dict, or an Exception the Activity raises."""

    @activity.defn(name="publish_migration_plan")
    async def publish_plan(request: dict) -> dict:
        if isinstance(publish, Exception):
            raise publish
        return publish

    @activity.defn(name="provision_target")
    async def scaffold(project_id: str) -> dict:
        return {}

    return Worker(
        client,
        task_queue=PROVISIONING_QUEUE,
        activities=[publish_plan, scaffold, emit_migration_event],
    )


async def run_migration(publish, scenario, *, workflow_id="migration-49"):
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            analysis_worker(env.client),
            sandbox_worker(env.client),
            build_worker(env.client),
            provisioning_worker(env.client, publish),
            create_activity_worker(env.client),
            create_workflow_worker(env.client),
        ):
            handle = await env.client.start_workflow(
                MigrationWorkflow.run, "project", id=workflow_id, task_queue=WORKFLOW_QUEUE
            )
            outcome = await scenario(env, handle)
            await Replayer(
                workflows=[
                    MigrationWorkflow,
                    AnalysisWorkflow,
                    SandboxVerificationWorkflow,
                    ProvisioningApprovalWorkflow,
                ]
            ).replay_workflow(await handle.fetch_history())
            return outcome


async def wait_phase(handle, phase):
    for _ in range(200):
        state = await handle.query("status")
        if state["phase"] == phase:
            return state
        await asyncio.sleep(0.05)
    pytest.fail(f"phase {phase} not reached: {state}")


# ---- Workflow: failure code, phase and issues are kept; one `error` event is emitted ----


async def test_plan_failure_keeps_code_and_issues_and_emits_one_error_event(emitter):
    async def scenario(env, handle):
        assert await handle.result() == "plan_failed"
        return await handle.query("status"), await read_migration_state(handle)

    state, api_state = await run_migration(LOCK_FAILURE, scenario)

    assert state["phase"] == "plan_failed"
    assert state["plan_code"] == "PLAN_FAILED"
    assert state["failure_code"] == "PLAN_FAILED"
    assert state["failure_phase"] == "planning"
    assert state["failure_issues"] == ["MISSING_PROVIDER_LOCK"]
    [error] = emitter.of_type("error")
    assert error.migration_id == "migration-49"
    assert (error.phase, error.status) == ("planning", "failed")
    assert error.data == {
        "code": "PLAN_FAILED",
        "message": "",
        "phase": "planning",
        "retryable": False,
    }
    [log] = [entry for entry in emitter.logs if entry["code"] == "MIGRATION_FAILED"]
    assert log["level"] == "error"
    assert log["fields"]["issues"] == ["MISSING_PROVIDER_LOCK"]
    assert log["fields"]["outcome"] == "plan_failed"
    assert api_state["phase"] == "plan_failed"
    assert api_state["execution_status"] == "COMPLETED"
    assert api_state["failure_issues"] == ["MISSING_PROVIDER_LOCK"]


async def test_unhandled_activity_failure_is_reported_and_workflow_stays_failed(emitter):
    crash = ApplicationError("boom with secret=hunter2", type="Crash", non_retryable=True)

    async def scenario(env, handle):
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        return await read_migration_state(handle)

    state = await run_migration(crash, scenario)

    assert state["execution_status"] == "FAILED"
    assert state["phase"] == "failed"
    assert state["failure_code"] == "ACTIVITY_FAILED"
    assert state["failure_phase"] == "planning"
    [error] = emitter.of_type("error")
    assert error.data["code"] == "ACTIVITY_FAILED"
    assert "hunter2" not in repr(emitter.events) + repr(emitter.logs)


async def test_successful_scaffold_run_emits_no_error(emitter):
    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await handle.signal("approve")
        return await handle.result()

    scaffold = {"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"}
    assert await run_migration(scaffold, scenario) == "scaffold_complete"
    assert emitter.of_type("error") == []


# ---- API state: Temporal execution status wins over a stale `running` phase ----


async def test_terminated_workflow_is_failed_not_running(emitter):
    scaffold = {"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"}

    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await handle.terminate(reason="operator")
        return await read_migration_state(handle)

    state = await run_migration(scaffold, scenario)

    assert state["execution_status"] == "TERMINATED"
    assert state["phase"] == "failed"
    assert state["approval_pending"] is False
    assert state["failure_code"] == "WORKFLOW_TERMINATED"
    assert state["failure_phase"] == "awaiting_approval"
    assert migration_status(state, None) == "failed"


async def test_closed_workflow_without_a_worker_is_read_from_describe(monkeypatch):
    monkeypatch.setattr(migration_state, "QUERY_TIMEOUT_SECONDS", 0.5)
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        handle = await env.client.start_workflow(
            MigrationWorkflow.run, "project", id="migration-orphan", task_queue="nobody"
        )
        await handle.terminate(reason="operator")
        state = await read_migration_state(handle)

    assert state["phase"] == "failed"
    assert state["failure_code"] == "WORKFLOW_TERMINATED"
    assert state["execution_status"] == "TERMINATED"
    assert migration_status(state, None) == "failed"


# ---- SSE phase mapping (N3) and snapshot phase ----


@pytest.mark.parametrize("phase", ["planning_handoff", "workload_planning", "created"])
def test_in_progress_phases_are_running_not_failed(phase):
    assert migration_status({"phase": phase}, None) == "running"


@pytest.mark.parametrize(
    ("phase", "web"), [("planning_handoff", "planning"), ("workload_planning", "planning")]
)
def test_snapshot_phase_follows_the_workflow_phase(phase, web):
    assert snapshot("m", "running", [], reset=True, seq=0, state={"phase": phase})["phase"] == web


def test_failed_snapshot_without_events_uses_the_failure_phase():
    state = {"phase": "plan_failed", "failure_phase": "planning"}
    assert snapshot("m", "failed", [], reset=True, seq=0, state=state)["phase"] == "planning"


# ---- execution marker (N7) ----


def test_claim_retry_from_the_same_run_is_claimed_again(db):
    approval_id = decide(db)
    with db() as session:
        first = claim_execution(session, approval_id, claimant="approval/run-1")
    with db() as session:
        again = claim_execution(session, approval_id, claimant="approval/run-1")
        other = claim_execution(session, approval_id, claimant="approval/run-2")
        anonymous = claim_execution(session, approval_id)
        marker = session.get(PlanExecution, ("plan-001", 1, EXECUTION_STEP))
    assert first["status"] == again["status"] == "CLAIMED"
    assert again == first
    assert other["status"] == anonymous["status"] == "ALREADY_EXECUTED"
    assert marker.claimed_by == "approval/run-1"


async def test_claim_activity_identifies_its_workflow_run(db):
    approval_id = decide(db)
    activities = ApprovalActivities(db)
    env = ActivityEnvironment()
    first = await env.run(activities.claim_approved_execution, approval_id)
    again = await env.run(activities.claim_approved_execution, approval_id)
    assert first["status"] == again["status"] == "CLAIMED"


async def run_approval(db, complete_failures: int | None):
    """Approval Workflow whose complete Activity fails `complete_failures` times (None: always)."""
    approval_id = decide(db)
    real = ApprovalActivities(db)
    attempts = {"n": 0}

    @activity.defn(name="complete_approved_execution")
    async def complete(result: dict) -> dict:
        attempts["n"] += 1
        if complete_failures is None or attempts["n"] <= complete_failures:
            raise RuntimeError("database unavailable")
        return await real.complete_approved_execution(result)

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
        Worker(
            env.client,
            task_queue=PROVISIONING_QUEUE,
            activities=[real.claim_approved_execution, complete, *FakeProvider().activities()],
        ),
    ):
        handle = await env.client.start_workflow(
            APPROVAL_WORKFLOW_NAME,
            {"plan_id": "plan-001", "revision": 1},
            id="approval-49",
            task_queue=WORKFLOW_QUEUE,
        )
        await handle.signal(APPROVAL_SIGNAL, approval_id)
        outcome = await asyncio.wait_for(handle.result(), timeout=60)
        await Replayer(workflows=[ProvisioningApprovalWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
    with db() as session:
        marker = execution_status(session, session.get(PlanApproval, approval_id))
    return outcome, marker, attempts["n"]


async def test_transient_complete_failures_still_finish_the_marker(db):
    outcome, marker, attempts = await run_approval(db, complete_failures=4)
    assert outcome["status"] == "EXECUTED"
    assert marker == "SUCCEEDED"
    assert attempts == 5


async def test_complete_that_never_succeeds_ends_with_a_code_not_a_crash(db):
    outcome, marker, _ = await run_approval(db, complete_failures=None)
    assert (outcome["status"], outcome["code"]) == ("UNKNOWN", "EXECUTION_RECORD_FAILED")
    assert marker == "RUNNING"  # provider may have run: still needs inspection, never re-run
