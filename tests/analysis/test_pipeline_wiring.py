"""MigrationWorkflow with a real analysis and scripted Sandbox Activities (ticket 21)."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from cmp_contracts.queues import ANALYSIS_QUEUE, SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker.activities.analysis import AnalysisActivities
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
from cmp_worker.workers.provisioning import create_worker as provisioning_worker
from cmp_worker.workflows.migration import MigrationWorkflow
from pipeline_support import analysis_input, make
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_integration import ROWS, mapping_llm

# The scripted Sandbox Activities live next to the Sandbox tests (package `sandbox`).
sys.path.insert(0, str(Path(__file__).parent.parent))
from sandbox.test_fix_loop import Script


def pipeline_input(repo, **sandbox):
    return {"project_id": "proj-1", "analysis": analysis_input(repo), "sandbox": sandbox or None}


async def until(handle, query, predicate, tries=300):
    for _ in range(tries):
        try:
            state = await handle.query(query)
        except RPCError:  # the child workflow may not have started yet
            await asyncio.sleep(0.02)
            continue
        if predicate(state):
            return state
        await asyncio.sleep(0.02)
    pytest.fail(f"{query} did not reach the expected state")


async def review(env, workflow_id):
    child = env.client.get_workflow_handle(f"analysis-{workflow_id}")
    state = await until(child, "review_state", lambda s: s["pending"] == "confirm_requirements")
    rows = {i["capability"]: i["id"] for i in state["items"]}
    ref = state["analysis_ref"]
    await child.signal(
        "confirm_requirements",
        {
            "analysis_ref": ref,
            "required": {rows["postgres"]: False, rows["environment-config"]: True},
            "actor": "alice",
        },
    )
    await until(child, "review_state", lambda s: s["pending"] == "approve_losses")
    await child.signal(
        "approve_losses",
        {"analysis_ref": ref, "approvals": {rows["postgres"]: "APPROVED"}, "actor": "bob"},
    )
    return rows


async def run_pipeline(repo, script, *, settings=None, name="a"):
    pipeline = make(repo, name, llm_factory=mapping_llm(*ROWS))
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(
            env.client, task_queue=ANALYSIS_QUEUE, activities=AnalysisActivities(pipeline).all()
        ),
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        build_worker(env.client),
        provisioning_worker(env.client),
        create_activity_worker(env.client),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            MigrationWorkflow.run, pipeline_input(repo, **(settings or {})), id="migration-w",
            task_queue=WORKFLOW_QUEUE,
        )  # fmt: skip
        rows = await review(env, "migration-w")
        status = await until(handle, "status", lambda s: s["phase"] == "awaiting_approval")
        await handle.signal("approve")
        result = await asyncio.wait_for(handle.result(), timeout=30)
        history = (await handle.fetch_history()).to_json()
        sandbox_input = json.loads(
            (await env.client.get_workflow_handle("sandbox-migration-w").fetch_history()).events[0]
            .workflow_execution_started_event_attributes.input.payloads[0].data
        )  # fmt: skip
    return result, status, rows, history, sandbox_input, pipeline


@pytest.mark.asyncio
async def test_the_analysis_result_reaches_the_sandbox_loop_and_the_pass_reaches_the_parent(repo):
    script = Script([None])
    result, status, rows, _, sandbox_input, _ = await run_pipeline(repo, script)
    assert result == "scaffold_complete"
    assert (
        sandbox_input["migration_id"] == "migration-w"
        and sandbox_input["requirements_ref"]["kind"] == "requirements"
    )
    assert sandbox_input["analysis_report_ref"]["kind"] == "analysis-report"
    assert sandbox_input["review_ref"]["kind"] == "review-record"
    assert sandbox_input["suite_ref"]["kind"] == "test-suite"
    assert sandbox_input["source"]["workspace"].endswith(repo["sha"])
    assert status["pipeline"]["sandbox_status"] == "PASSED"
    assert status["pipeline"]["requirements_ref"] == sandbox_input["requirements_ref"]
    assert status["pipeline"]["published"]["status"] == "PUBLISHED"
    # every loop step saw the same inputs the analysis produced
    build = next(p for n, p in script.calls if n == "sbx_build")
    assert build["requirements_ref"] == sandbox_input["requirements_ref"]
    plan = next(p for n, p in script.calls if n == "sbx_plan_publish")
    assert plan["input"]["review_ref"] == sandbox_input["review_ref"]
    assert set(rows) == {"postgres", "environment-config"}


@pytest.mark.asyncio
async def test_a_failed_loop_ends_the_parent_as_failed_with_the_reason_code(repo):
    script = Script(["RENDER"])
    pipeline = make(repo, "f", llm_factory=mapping_llm(*ROWS))
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(
            env.client, task_queue=ANALYSIS_QUEUE, activities=AnalysisActivities(pipeline).all()
        ),
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        build_worker(env.client),
        provisioning_worker(env.client),
        create_activity_worker(env.client),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            MigrationWorkflow.run,
            pipeline_input(repo),
            id="migration-f",
            task_queue=WORKFLOW_QUEUE,
        )
        await review(env, "migration-f")
        assert await asyncio.wait_for(handle.result(), timeout=30) == "failed"
        status = await handle.query("status")
    assert (
        status["failure_code"] == "STOPPED_RENDER"
        and status["pipeline"]["sandbox_status"] == "STOPPED_RENDER"
    )


@pytest.mark.asyncio
async def test_settings_for_the_sandbox_reach_the_loop_input(repo):
    script = Script([None])
    *_, sandbox_input, _ = await run_pipeline(
        repo, script, settings={"run_id": "fixed-run", "storage_class": "fast"}, name="s"
    )
    assert sandbox_input["run_id"] == "fixed-run" and sandbox_input["storage_class"] == "fast"


@pytest.mark.asyncio
async def test_the_history_holds_references_and_paths_only(repo):
    from pipeline_support import MAIN_GO, SECRET

    script = Script([None])
    _, _, _, history, sandbox_input, _ = await run_pipeline(repo, script, name="h")
    assert (
        SECRET not in history
        and "package main" not in history
        and MAIN_GO.splitlines()[10] not in history
    )
    assert len(json.dumps(sandbox_input)) < 4000


@pytest.mark.asyncio
async def test_a_string_start_still_runs_the_scaffold_and_never_asks_for_a_sandbox_input():
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        from cmp_worker.workers.analysis import create_worker as analysis_worker
        from cmp_worker.workers.sandbox import create_worker as sandbox_worker

        async with (
            analysis_worker(env.client), sandbox_worker(env.client), build_worker(env.client),
            provisioning_worker(env.client), create_activity_worker(env.client),
            create_workflow_worker(env.client),
        ):  # fmt: skip
            handle = await env.client.start_workflow(
                MigrationWorkflow.run, "project", id="migration-str", task_queue=WORKFLOW_QUEUE
            )
            await until(handle, "status", lambda s: s["phase"] == "awaiting_approval")
            assert (await handle.query("status"))["pipeline"] is None
            await handle.signal("approve")
            assert await asyncio.wait_for(handle.result(), timeout=30) == "scaffold_complete"
            names = [
                e.activity_task_scheduled_event_attributes.activity_type.name
                for e in (await handle.fetch_history()).events
                if e.HasField("activity_task_scheduled_event_attributes")
            ]
    assert "assemble_sandbox_input" not in names
