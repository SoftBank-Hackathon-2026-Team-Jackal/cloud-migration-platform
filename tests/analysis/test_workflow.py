import asyncio
import os
import re
from pathlib import Path

import pytest
from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_contracts.queues import ANALYSIS_QUEUE, WORKFLOW_QUEUE
from cmp_worker.activities import analysis as analysis_activities
from cmp_worker.workers.analysis import create_worker as analysis_worker
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_workflow_worker
from cmp_worker.workers.provisioning import create_worker as provisioning_worker
from cmp_worker.workers.sandbox import create_worker as sandbox_worker
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker


@activity.defn(name="fetch_source")
async def blocked_fetch(migration_id: str) -> str:
    while True:
        activity.heartbeat()
        await asyncio.sleep(0.05)
    return migration_id


@activity.defn(name="fetch_source")
async def failed_fetch(migration_id: str) -> str:
    raise ApplicationError("fixture failure", non_retryable=True)


@activity.defn(name="build_requirements")
async def invalid_requirements(migration_id: str) -> dict:
    return {"contract_version": "2", "resources": [], "unexpected": True}


@workflow.defn(name="AnalysisWorkflow")
class SlowCancelAnalysis:
    @workflow.run
    async def run(self, migration_id: str) -> dict:
        try:
            await workflow.wait_condition(lambda: False)
        except asyncio.CancelledError:
            await workflow.wait_condition(lambda: False)
        return {}


@pytest.mark.asyncio
async def test_legacy_history_replays():
    history = WorkflowHistory.from_json(
        "legacy-history-fixture",
        Path("tests/fixtures/analysis/migration-before-02.json").read_text(),
    )
    await Replayer(workflows=[MigrationWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_legacy_approval_wait_history_replays():
    history = WorkflowHistory.from_json(
        "legacy-awaiting-fixture",
        Path("tests/fixtures/analysis/migration-before-02-awaiting.json").read_text(),
    )
    await Replayer(workflows=[MigrationWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_analysis_child_runs_on_migration_queue():
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
                MigrationWorkflow.run, "migration-test", id="parent-test", task_queue=WORKFLOW_QUEUE
            )
            while (await handle.query("status"))["phase"] != "awaiting_approval":
                await env.sleep(0.1)
            await handle.signal(MigrationWorkflow.request_cancel)
            assert await handle.result() == "cancelled"
            history = await handle.fetch_history()
            starts = [
                e.start_child_workflow_execution_initiated_event_attributes
                for e in history.events
                if e.HasField("start_child_workflow_execution_initiated_event_attributes")
                and e.start_child_workflow_execution_initiated_event_attributes.workflow_type.name
                == "AnalysisWorkflow"
            ]
            assert len(starts) == 1
            assert starts[0].workflow_id == "analysis-parent-test"
            assert starts[0].task_queue.name == WORKFLOW_QUEUE
            assert starts[0].parent_close_policy == 1  # TERMINATE
            child_result = await env.client.get_workflow_handle("analysis-parent-test").result()
            assert child_result["report"]["migration_id"] == "parent-test"
            assert any(
                e.HasField("marker_recorded_event_attributes")
                and e.marker_recorded_event_attributes.marker_name == "core_patch"
                for e in history.events
            )
            await Replayer(workflows=[MigrationWorkflow, AnalysisWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_same_project_can_have_two_running_analyses():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            Worker(env.client, task_queue=ANALYSIS_QUEUE, activities=[blocked_fetch]),
            create_workflow_worker(env.client),
        ):
            parents = [
                await env.client.start_workflow(
                    MigrationWorkflow.run, "same-project", id=mid, task_queue=WORKFLOW_QUEUE
                )
                for mid in ("migration-one", "migration-two")
            ]
            for parent in parents:
                for _ in range(100):
                    history = await parent.fetch_history()
                    if any(
                        e.HasField("child_workflow_execution_started_event_attributes")
                        for e in history.events
                    ):
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("analysis child did not start")
            for mid in ("migration-one", "migration-two"):
                assert (
                    await env.client.get_workflow_handle(f"analysis-{mid}").describe()
                ).status.name == "RUNNING"
            for parent in parents:
                await parent.signal(MigrationWorkflow.request_cancel)
            assert await asyncio.gather(*(parent.result() for parent in parents)) == [
                "cancelled",
                "cancelled",
            ]


@pytest.mark.asyncio
async def test_analysis_fixture_flow_and_parallel_scans(tmp_path, monkeypatch):
    secret = "fixture-secret-value-never-serialize"
    (tmp_path / ".env").write_text(f"PASSWORD={secret}\n")
    monkeypatch.chdir(tmp_path)
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with analysis_worker(env.client), create_workflow_worker(env.client):
            handle = await env.client.start_workflow(
                AnalysisWorkflow.run,
                "fixture-migration",
                id="analysis-fixture",
                task_queue=WORKFLOW_QUEUE,
            )
            result = await handle.result()
            assert (
                AnalysisReport.model_validate(result["report"]).migration_id == "fixture-migration"
            )
            RequirementsV2.model_validate(result["requirements"])
            history = await handle.fetch_history()
            serialized = b"".join(e.SerializeToString() for e in history.events)
            assert secret.encode() not in serialized
            assert not re.search(
                rb"AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", serialized
            )
            scheduled = [
                (i, e.activity_task_scheduled_event_attributes.activity_type.name)
                for i, e in enumerate(history.events)
                if e.HasField("activity_task_scheduled_event_attributes")
            ]
            scans = [(i, n) for i, n in scheduled if n.startswith("scan_")]
            assert {n for _, n in scans} == {
                "scan_code",
                "scan_manifests",
                "scan_aws_infra",
                "scan_network",
            }
            completed = [
                i
                for i, e in enumerate(history.events)
                if e.HasField("activity_task_completed_event_attributes")
            ]
            assert max(i for i, _ in scans) < completed[1]


@pytest.mark.asyncio
async def test_invalid_requirements_rejected_across_activity_boundary():
    activities = [
        analysis_activities.fetch_source,
        analysis_activities.scan_code,
        analysis_activities.scan_manifests,
        analysis_activities.scan_aws_infra,
        analysis_activities.scan_network,
        invalid_requirements,
        analysis_activities.precheck,
        analysis_activities.map_usage,
        analysis_activities.check_evidence,
    ]
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            Worker(env.client, task_queue=ANALYSIS_QUEUE, activities=activities),
            create_workflow_worker(env.client),
        ):
            handle = await env.client.start_workflow(
                AnalysisWorkflow.run,
                "invalid-fixture",
                id="analysis-invalid",
                task_queue=WORKFLOW_QUEUE,
            )
            with pytest.raises(WorkflowFailureError):
                await handle.result()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_child_failure_or_parent_cancel(fail):
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=ANALYSIS_QUEUE,
                activities=[failed_fetch if fail else blocked_fetch],
            ),
            create_workflow_worker(env.client),
        ):
            handle = await env.client.start_workflow(
                MigrationWorkflow.run,
                "cancel-fixture",
                id=f"failure-test-{fail}",
                task_queue=WORKFLOW_QUEUE,
            )
            child = env.client.get_workflow_handle(f"analysis-failure-test-{fail}")
            while True:
                history = await handle.fetch_history()
                if any(
                    e.HasField("child_workflow_execution_started_event_attributes")
                    for e in history.events
                ):
                    break
                await env.sleep(0.1)
            if fail:
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
                assert (await handle.query("status"))["phase"] == "analysis"
            else:
                await handle.signal(MigrationWorkflow.request_cancel)
                assert await handle.result() == "cancelled"
                with pytest.raises(WorkflowFailureError):
                    await child.result()
            history = await handle.fetch_history()
            assert not any(
                e.HasField("activity_task_scheduled_event_attributes")
                and e.activity_task_scheduled_event_attributes.activity_type.name == "prepare_image"
                for e in history.events
            )


@pytest.mark.asyncio
async def test_external_parent_cancellation_cancels_child():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            Worker(env.client, task_queue=ANALYSIS_QUEUE, activities=[blocked_fetch]),
            create_workflow_worker(env.client),
        ):
            parent = await env.client.start_workflow(
                MigrationWorkflow.run,
                "external-cancel",
                id="external-parent",
                task_queue=WORKFLOW_QUEUE,
            )
            while True:
                history = await parent.fetch_history()
                if any(
                    e.HasField("child_workflow_execution_started_event_attributes")
                    for e in history.events
                ):
                    break
                await asyncio.sleep(0.05)
            child = env.client.get_workflow_handle("analysis-external-parent")
            await parent.cancel()
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(parent.result(), timeout=5)
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(child.result(), timeout=5)


@pytest.mark.asyncio
async def test_external_cancel_during_signal_cancellation_is_not_swallowed():
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[MigrationWorkflow, SlowCancelAnalysis],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            parent = await env.client.start_workflow(
                MigrationWorkflow.run, "project", id="migration-race", task_queue=WORKFLOW_QUEUE
            )
            while True:
                history = await parent.fetch_history()
                if any(
                    e.HasField("child_workflow_execution_started_event_attributes")
                    for e in history.events
                ):
                    break
                await asyncio.sleep(0.05)
            await parent.signal(MigrationWorkflow.request_cancel)
            for _ in range(100):
                history = await parent.fetch_history()
                if any(
                    e.HasField(
                        "request_cancel_external_workflow_execution_initiated_event_attributes"
                    )
                    for e in history.events
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("child cancellation was not requested")
            await parent.cancel()
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(parent.result(), timeout=5)
            assert (await parent.describe()).status.name == "CANCELED"
