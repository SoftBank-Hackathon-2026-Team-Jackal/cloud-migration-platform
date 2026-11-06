"""Ticket 49 follow-up (D-19): closed runs are judged correctly while no worker can answer queries.

Seam: Temporal time-skipping server; the run finishes with real workers, the workers stop, then
`read_migration_state` reads it with the stored last `error` event (InMemoryEmitter) and the
Workflow result. The SSE route is checked with a fake status reader over SQLite.
"""

import os
import time

import pytest
from cmp_api import migration_state
from cmp_api.event_stream import migration_status
from cmp_api.migration_events import InMemoryEmitter
from cmp_api.migration_state import read_migration_state
from cmp_contracts.queues import WORKFLOW_QUEUE
from cmp_worker.events import configure_emitter
from cmp_worker.workers.analysis import create_worker as analysis_worker
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
from cmp_worker.workers.sandbox import create_worker as sandbox_worker
from cmp_worker.workflows.migration import MigrationWorkflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from test_failure_visibility import LOCK_FAILURE, provisioning_worker, wait_phase

MISSING_INPUT = {"status": "FAILED", "code": "PLANNING_INPUT_MISSING"}
SCAFFOLD = {"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"}


@pytest.fixture
def emitter():
    stored = InMemoryEmitter()
    previous = configure_emitter(stored)
    yield stored
    configure_emitter(previous)


@pytest.fixture(autouse=True)
def short_query(monkeypatch):
    monkeypatch.setattr(migration_state, "QUERY_TIMEOUT_SECONDS", 0.5)


def last_error_from(emitter):
    def read(migration_id: str) -> dict | None:
        errors = [e for e in emitter.of_type("error") if e.migration_id == migration_id]
        return errors[-1].model_dump() if errors else None

    return read


async def finish_then_stop_workers(publish, finish, *, emitter, use_events=True):
    """Run to the end with workers, stop every worker, then read the state."""
    async with await WorkflowEnvironment.start_time_skipping(
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
                MigrationWorkflow.run, "project", id="migration-d19", task_queue=WORKFLOW_QUEUE
            )
            await finish(handle)
        reader = last_error_from(emitter) if use_events else None
        return await read_migration_state(handle, last_error=reader)


async def result(handle):
    return await handle.result()


async def test_completed_plan_failure_keeps_code_and_phase_without_a_worker(emitter):
    state = await finish_then_stop_workers(MISSING_INPUT, result, emitter=emitter)

    assert state["execution_status"] == "COMPLETED"
    assert state["phase"] == "plan_failed"
    assert state["failure_code"] == "PLANNING_INPUT_MISSING"
    assert state["failure_phase"] == "planning"
    assert migration_status(state, None) == "failed"


async def test_completed_failure_without_stored_events_uses_the_workflow_result(emitter):
    state = await finish_then_stop_workers(LOCK_FAILURE, result, emitter=emitter, use_events=False)

    assert state["phase"] == "plan_failed"
    assert state["failure_code"] == "PLAN_FAILED"
    assert state["failure_phase"] == "planning"
    assert migration_status(state, None) == "failed"


async def test_completed_success_without_a_worker_is_done(emitter):
    async def approve(handle):
        await wait_phase(handle, "awaiting_approval")
        await handle.signal("approve")
        await handle.result()

    state = await finish_then_stop_workers(SCAFFOLD, approve, emitter=emitter)

    assert state["phase"] == "scaffold_complete"
    assert state.get("failure_code") is None
    assert migration_status(state, None) == "done"


async def test_failed_run_without_a_worker_keeps_the_reported_code(emitter):
    crash = ApplicationError("boom", type="Crash", non_retryable=True)

    async def fails(handle):
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    state = await finish_then_stop_workers(crash, fails, emitter=emitter)

    assert state["execution_status"] == "FAILED"
    assert state["phase"] == "failed"
    assert state["failure_code"] == "ACTIVITY_FAILED"  # not the generic WORKFLOW_FAILED
    assert state["failure_phase"] == "planning"


def test_a_closed_run_is_never_running_whatever_its_phase():
    stale = {"phase": "created", "execution_status": "COMPLETED"}
    assert migration_status(stale, None) == "failed"
    assert migration_status({"phase": "planning", "execution_status": "TERMINATED"}, None) == (
        "failed"
    )
    assert migration_status({"phase": "planning", "execution_status": "RUNNING"}, None) == (
        "running"
    )


async def test_running_run_without_a_worker_fails_fast_instead_of_hanging():
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        handle = await env.client.start_workflow(
            MigrationWorkflow.run, "project", id="migration-noworker", task_queue="nobody"
        )
        started = time.monotonic()
        with pytest.raises(Exception):  # noqa: B017 - RPC deadline; callers map it to 503/fallback
            await read_migration_state(handle)
        assert time.monotonic() - started < 10
