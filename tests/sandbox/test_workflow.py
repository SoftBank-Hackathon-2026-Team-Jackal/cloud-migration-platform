import asyncio
import json
import os
from pathlib import Path

import pytest
from cmp_contracts.queues import SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker.activities import sandbox
from cmp_worker.workers.analysis import create_worker as analysis_worker
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_workflow_worker
from cmp_worker.workers.provisioning import create_worker as provisioning_worker
from cmp_worker.workers.sandbox import create_worker as sandbox_worker
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker


def scenario_worker(client, failures, *, block_tests=False, teardown_gate=None):
    outcomes = iter(failures + [None])

    @activity.defn(name="judge_sandbox")
    async def judge(payload: dict) -> dict:
        failure = next(outcomes)
        return {"passed": failure is None, "failure_class": failure}

    @activity.defn(name="run_sandbox_tests")
    async def blocked_tests(payload: dict) -> dict:
        while True:
            await asyncio.sleep(0.05)

    @activity.defn(name="teardown_sandbox")
    async def delayed_teardown(payload: dict) -> dict:
        await teardown_gate.wait()
        return {"cleanup_status": "CLEANED"}

    return Worker(
        client,
        task_queue=SANDBOX_QUEUE,
        activities=[
            sandbox.request_build,
            sandbox.prepare_sandbox,
            sandbox.render_sandbox,
            sandbox.apply_sandbox,
            blocked_tests if block_tests else sandbox.run_sandbox_tests,
            judge,
            sandbox.classify_sandbox_failure,
            sandbox.fix_sandbox,
            sandbox.publish_final_report,
            delayed_teardown if teardown_gate is not None else sandbox.teardown_sandbox,
        ],
    )


def scheduled_names(history):
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]


@pytest.mark.asyncio
async def test_before_02b_history_replays():
    history = WorkflowHistory.from_json(
        "legacy-history-fixture",
        Path("tests/fixtures/analysis/migration-before-02b.json").read_text(),
    )
    await Replayer(workflows=[MigrationWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_before_14b_stopped_render_history_replays():
    """A parent that ended as `stopped_render` before 14B still replays (no patch marker)."""
    history = WorkflowHistory.from_json(
        "parent-render",
        Path("tests/fixtures/analysis/migration-before-14b-stopped-render.json").read_text(),
    )
    await Replayer(
        workflows=[MigrationWorkflow, AnalysisWorkflow, SandboxVerificationWorkflow]
    ).replay_workflow(history)


@pytest.mark.asyncio
async def test_sandbox_scaffold_passes_and_cleans_up():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with sandbox_worker(env.client), create_workflow_worker(env.client):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow",
                "migration-test",
                id="sandbox-test",
                task_queue=WORKFLOW_QUEUE,
            )
            assert (await asyncio.wait_for(handle.result(), timeout=2))["status"] == "PASSED"
            history = await handle.fetch_history()
            names = [
                e.activity_task_scheduled_event_attributes.activity_type.name
                for e in history.events
                if e.HasField("activity_task_scheduled_event_attributes")
            ]
            assert names[-1] == "teardown_sandbox"


@pytest.mark.asyncio
async def test_parent_runs_sandbox_after_analysis_before_build():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            analysis_worker(env.client),
            sandbox_worker(env.client),
            build_worker(env.client),
            provisioning_worker(env.client),
            create_workflow_worker(env.client),
        ):
            handle = await env.client.start_workflow(
                MigrationWorkflow.run, "project", id="migration-parent", task_queue=WORKFLOW_QUEUE
            )
            for _ in range(100):
                if (await handle.query("status"))["phase"] == "awaiting_approval":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("approval not reached")
            await handle.signal("request_cancel")
            assert await handle.result() == "cancelled"
            history = await handle.fetch_history()
            starts = [
                e.start_child_workflow_execution_initiated_event_attributes
                for e in history.events
                if e.HasField("start_child_workflow_execution_initiated_event_attributes")
            ]
            assert [item.workflow_id for item in starts] == [
                "analysis-migration-parent",
                "sandbox-migration-parent",
            ]
            assert starts[1].task_queue.name == WORKFLOW_QUEUE
            await Replayer(
                workflows=[MigrationWorkflow, AnalysisWorkflow, SandboxVerificationWorkflow]
            ).replay_workflow(history)


@pytest.mark.asyncio
async def test_parent_stops_when_sandbox_render_fails():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            analysis_worker(env.client),
            scenario_worker(env.client, ["RENDER"]),
            create_workflow_worker(env.client),
        ):
            handle = await env.client.start_workflow(
                MigrationWorkflow.run, "project", id="parent-render", task_queue=WORKFLOW_QUEUE
            )
            assert await asyncio.wait_for(handle.result(), timeout=5) == "failed"
            assert (await handle.query("status"))["failure_code"] == "STOPPED_RENDER"
            history = await handle.fetch_history()
            assert "prepare_image" not in scheduled_names(history)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "builds"), [("SPEC", 1), ("CODE", 2), ("BUILD", 2), ("INFRA", 1)]
)
async def test_single_failure_retry(failure, builds):
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with scenario_worker(env.client, [failure]), create_workflow_worker(env.client):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow", "mig", id="retry", task_queue=WORKFLOW_QUEUE
            )
            result = await asyncio.wait_for(handle.result(), timeout=5)
            assert result["status"] == "PASSED"
            names = scheduled_names(await handle.fetch_history())
            assert names.count("request_build") == builds
            assert names.count("judge_sandbox") == 2
            assert names.count("fix_sandbox") == (0 if failure == "INFRA" else 1)
            judge_attempts = [
                json.loads(e.activity_task_scheduled_event_attributes.input.payloads[0].data)[
                    "attempt"
                ]
                for e in (await handle.fetch_history()).events
                if e.HasField("activity_task_scheduled_event_attributes")
                and e.activity_task_scheduled_event_attributes.activity_type.name == "judge_sandbox"
            ]
            assert judge_attempts == ([1, 1] if failure == "INFRA" else [1, 2])


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [["SPEC", "SPEC", "SPEC"], ["INFRA", "INFRA"]])
async def test_review_wait_after_cleanup(failures):
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with scenario_worker(env.client, failures), create_workflow_worker(env.client):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow", "mig", id="review", task_queue=WORKFLOW_QUEUE
            )
            for _ in range(100):
                status = await handle.query("status")
                if status["phase"] == "NEEDS_REVIEW":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("review wait not reached")
            assert status["attempt"] == (1 if failures[0] == "INFRA" else 3)
            names = scheduled_names(await handle.fetch_history())
            assert names.count("teardown_sandbox") == 1
            await handle.signal("review")
            assert (await asyncio.wait_for(handle.result(), timeout=5))["status"] == "NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_render_failure_stops_without_fix():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with scenario_worker(env.client, ["RENDER"]), create_workflow_worker(env.client):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow", "mig", id="render", task_queue=WORKFLOW_QUEUE
            )
            assert (await asyncio.wait_for(handle.result(), timeout=5))[
                "status"
            ] == "STOPPED_RENDER"
            names = scheduled_names(await handle.fetch_history())
            assert "fix_sandbox" not in names
            assert names[-1] == "teardown_sandbox"


@pytest.mark.asyncio
async def test_review_signal_before_wait_does_not_bypass_gate():
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow", "mig", id="early-review", task_queue=WORKFLOW_QUEUE
        )
        await handle.signal("review")
        async with (
            scenario_worker(env.client, ["INFRA", "INFRA"]),
            create_workflow_worker(env.client),
        ):
            for _ in range(100):
                if (await handle.query("status"))["phase"] == "NEEDS_REVIEW":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("review wait not reached")
            assert (await handle.describe()).status.name == "RUNNING"
            await handle.signal("review")
            assert (await asyncio.wait_for(handle.result(), timeout=5))["status"] == "NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_external_cancellation_runs_teardown_to_completion():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            scenario_worker(env.client, [], block_tests=True),
            create_workflow_worker(env.client),
        ):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow", "mig", id="cancel", task_queue=WORKFLOW_QUEUE
            )
            for _ in range(100):
                if (await handle.query("status"))["phase"] == "run_sandbox_tests":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("test phase was not reached")
            await handle.cancel()
            from temporalio.client import WorkflowFailureError

            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), timeout=5)
            history = await handle.fetch_history()
            scheduled = {
                e.event_id: e.activity_task_scheduled_event_attributes.activity_type.name
                for e in history.events
                if e.HasField("activity_task_scheduled_event_attributes")
            }
            assert any(
                e.HasField("activity_task_completed_event_attributes")
                and scheduled.get(e.activity_task_completed_event_attributes.scheduled_event_id)
                == "teardown_sandbox"
                for e in history.events
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["signal", "external", "both"])
async def test_parent_cancel_waits_for_sandbox_cleanup(mode):
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        gate = asyncio.Event()
        async with (
            analysis_worker(env.client),
            scenario_worker(env.client, [], block_tests=True, teardown_gate=gate),
            create_workflow_worker(env.client),
        ):
            parent = await env.client.start_workflow(
                MigrationWorkflow.run, "project", id="migration-cleanup", task_queue=WORKFLOW_QUEUE
            )
            child = env.client.get_workflow_handle("sandbox-migration-cleanup")
            for _ in range(100):
                history = await parent.fetch_history()
                starts = [
                    e.child_workflow_execution_started_event_attributes
                    for e in history.events
                    if e.HasField("child_workflow_execution_started_event_attributes")
                ]
                if (
                    any(
                        item.workflow_execution.workflow_id == "sandbox-migration-cleanup"
                        for item in starts
                    )
                    and (await child.query("status"))["phase"] == "run_sandbox_tests"
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("sandbox test phase not reached")
            if mode == "external":
                from temporalio.client import WorkflowFailureError

                await parent.cancel()
            else:
                await parent.signal("request_cancel")
            for _ in range(100):
                if "teardown_sandbox" in scheduled_names(await child.fetch_history()):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("cleanup was not scheduled")
            if mode == "both":
                from temporalio.client import WorkflowFailureError

                await parent.cancel()
            assert (await parent.describe()).status.name == "RUNNING"
            gate.set()
            if mode != "signal":
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(parent.result(), timeout=5)
            else:
                assert await asyncio.wait_for(parent.result(), timeout=5) == "cancelled"
            from temporalio.client import WorkflowFailureError

            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(child.result(), timeout=5)
            history = await child.fetch_history()
            scheduled = {
                e.event_id: e.activity_task_scheduled_event_attributes.activity_type.name
                for e in history.events
                if e.HasField("activity_task_scheduled_event_attributes")
            }
            assert any(
                e.HasField("activity_task_completed_event_attributes")
                and scheduled.get(e.activity_task_completed_event_attributes.scheduled_event_id)
                == "teardown_sandbox"
                for e in history.events
            )


@pytest.mark.asyncio
async def test_unknown_failure_class_fails_workflow_and_cleans_up():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with scenario_worker(env.client, ["WAT"]), create_workflow_worker(env.client):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow", "mig", id="unknown", task_queue=WORKFLOW_QUEUE
            )
            with pytest.raises(WorkflowFailureError) as exc:
                await asyncio.wait_for(handle.result(), timeout=5)
            assert exc.value.cause.type == "UnknownFailureClass"
            names = scheduled_names(await handle.fetch_history())
            assert "fix_sandbox" not in names
            assert names[-1] == "teardown_sandbox"
            assert (await handle.describe()).status.name == "FAILED"


@pytest.mark.asyncio
async def test_sandbox_activities_have_schedule_to_start_bound():
    # The time-skipping test server does not advance activity schedule-to-start
    # timers, so assert the bound is set on every scheduled activity instead.
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with scenario_worker(env.client, []), create_workflow_worker(env.client):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow", "mig", id="bounds", task_queue=WORKFLOW_QUEUE
            )
            await asyncio.wait_for(handle.result(), timeout=5)
            scheduled = [
                e.activity_task_scheduled_event_attributes
                for e in (await handle.fetch_history()).events
                if e.HasField("activity_task_scheduled_event_attributes")
            ]
            assert scheduled
            assert all(a.schedule_to_start_timeout.seconds == 300 for a in scheduled)


def failing_worker(client, *, build_fails=False):
    @activity.defn(name="request_build")
    async def bad_build(payload: dict) -> dict:
        raise ApplicationError("build unavailable", type="BuildDown", non_retryable=True)

    @activity.defn(name="teardown_sandbox")
    async def bad_teardown(payload: dict) -> dict:
        raise ApplicationError("cleanup unavailable", type="CleanupDown", non_retryable=True)

    @activity.defn(name="judge_sandbox")
    async def judge(payload: dict) -> dict:
        return {"passed": False, "failure_class": "WAT"}

    return Worker(
        client,
        task_queue=SANDBOX_QUEUE,
        activities=[
            bad_build if build_fails else sandbox.request_build,
            sandbox.prepare_sandbox,
            sandbox.render_sandbox,
            sandbox.apply_sandbox,
            sandbox.run_sandbox_tests,
            judge,
            sandbox.classify_sandbox_failure,
            sandbox.fix_sandbox,
            sandbox.publish_final_report,
            bad_teardown,
        ],
    )


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_hide_original_error():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with failing_worker(env.client), create_workflow_worker(env.client):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow", "mig", id="cleanup-fails", task_queue=WORKFLOW_QUEUE
            )
            with pytest.raises(WorkflowFailureError) as exc:
                await asyncio.wait_for(handle.result(), timeout=5)
            assert exc.value.cause.type == "UnknownFailureClass"
            assert (await handle.query("status"))["cleanup_failed"] is True


@pytest.mark.asyncio
async def test_no_teardown_when_failure_precedes_sandbox_preparation():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            failing_worker(env.client, build_fails=True),
            create_workflow_worker(env.client),
        ):
            handle = await env.client.start_workflow(
                "SandboxVerificationWorkflow", "mig", id="build-down", task_queue=WORKFLOW_QUEUE
            )
            with pytest.raises(WorkflowFailureError) as exc:
                await asyncio.wait_for(handle.result(), timeout=5)
            assert exc.value.cause.cause.type == "BuildDown"
            assert "teardown_sandbox" not in scheduled_names(await handle.fetch_history())
