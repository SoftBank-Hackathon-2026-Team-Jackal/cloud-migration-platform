"""MigrationWorkflow with a real analysis and scripted Sandbox Activities (ticket 21)."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from cmp_contracts.queues import ANALYSIS_QUEUE, SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_providers.env_file import InMemoryEnvFileStore
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


def pipeline_input(repo, planning=None, env_file_ref=None, **sandbox):
    data = {"project_id": "proj-1", "analysis": analysis_input(repo), "sandbox": sandbox or None}
    data = {**data, "env_file_ref": env_file_ref} if env_file_ref else data
    return {**data, "planning": planning} if planning else data


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


async def run_pipeline(
    repo, script, *, settings=None, name="a", planning=None, env_file_ref=None, histories=None
):
    pipeline = make(repo, name, llm_factory=mapping_llm(*ROWS), env_store=ENV_STORE)
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
            MigrationWorkflow.run, pipeline_input(repo, planning, env_file_ref, **(settings or {})),
            id="migration-w", task_queue=WORKFLOW_QUEUE,
        )  # fmt: skip
        rows = await review(env, "migration-w")
        status = await until(handle, "status", lambda s: s["phase"] == "awaiting_approval")
        await handle.signal("approve")
        result = await asyncio.wait_for(handle.result(), timeout=30)
        history = (await handle.fetch_history()).to_json()
        if histories is not None:
            for workflow_id in ("migration-w", "analysis-migration-w", "sandbox-migration-w"):
                histories[workflow_id] = await env.client.get_workflow_handle(
                    workflow_id
                ).fetch_history()
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
async def test_analysis_events_use_the_parent_migration_id_and_done_is_sent_once(repo):
    """Ticket 58: findings and one `analysis.done` are stored under the MigrationWorkflow ID."""
    from cmp_api.migration_events import InMemoryEmitter
    from cmp_worker.events import configure_emitter

    stored = InMemoryEmitter()
    previous = configure_emitter(stored)
    try:
        await run_pipeline(repo, Script([None]), name="e")
    finally:
        configure_emitter(previous)
    analysis = [e for e in stored.events if e.phase == "analysis"]
    assert analysis and {e.migration_id for e in analysis} == {"migration-w"}
    assert [e.type for e in analysis].count("analysis.done") == 1
    finding_ids = [e.data["id"] for e in analysis if e.type == "analysis.finding"]
    assert len(finding_ids) == len(set(finding_ids))


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


# ------------------------------------------------------------------ ticket 15: hand-off


class HandoffScript(Script):
    def __init__(self, *a, refuse=None, **kw):
        super().__init__(*a, **kw)
        self.refuse = refuse

    def activities(self):
        from temporalio import activity

        script = self

        @activity.defn(name="sbx_plan_assemble")
        async def assemble(payload: dict) -> dict:
            script.record("sbx_plan_assemble", payload)
            if script.refuse:
                return {"status": "REFUSED", "code": script.refuse}
            return {
                "status": "READY",
                "registration_ref": {"id": "planning-input", "sha256": "a" * 64},
            }

        return [*super().activities(), assemble]


class FakeRegister:
    def __init__(self):
        self.calls = []
        from temporalio import activity

        outer = self

        @activity.defn(name="register_planning_input")
        async def register(payload: dict) -> dict:
            outer.calls.append(payload)
            return {"status": "REGISTERED", "input_digest": "d" * 64}

        self.register_planning_input = register


def planning_settings():
    from sandbox.test_handoff import evidence

    return evidence()[0]["settings"].model_dump(mode="json")


@pytest.mark.asyncio
async def test_a_passed_run_is_handed_to_planning_before_planning_starts(repo, monkeypatch):
    fake = FakeRegister()
    monkeypatch.setattr(
        "cmp_worker.workers.provisioning.build_planning_register_activities", lambda _db: fake
    )
    script = HandoffScript([None])
    result, status, *_ = await run_pipeline(repo, script, name="p", planning=planning_settings())
    assert result == "scaffold_complete"
    assert status["pipeline"]["planning_input_digest"] == "d" * 64
    assemble = next(p for n, p in script.calls if n == "sbx_plan_assemble")
    assert (
        assemble["suite_ref"]["kind"] == "test-suite" and assemble["migration_id"] == "migration-w"
    )
    assert assemble["sandbox"]["published"]["status"] == "PUBLISHED"
    assert fake.calls and fake.calls[0]["migration_id"] == "migration-w"


@pytest.mark.asyncio
async def test_a_refused_hand_off_ends_the_parent_as_failed_with_the_code(repo, monkeypatch):
    fake = FakeRegister()
    monkeypatch.setattr(
        "cmp_worker.workers.provisioning.build_planning_register_activities", lambda _db: fake
    )
    script = HandoffScript([None], refuse="BUILD_EVIDENCE_MISSING")
    pipeline = make(repo, "r", llm_factory=mapping_llm(*ROWS))
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
            MigrationWorkflow.run, pipeline_input(repo, planning_settings()),
            id="migration-r", task_queue=WORKFLOW_QUEUE,
        )  # fmt: skip
        await review(env, "migration-r")
        assert await asyncio.wait_for(handle.result(), timeout=30) == "failed"
        status = await handle.query("status")
    assert status["failure_code"] == "BUILD_EVIDENCE_MISSING" and fake.calls == []


@pytest.mark.asyncio
async def test_without_planning_settings_nothing_is_handed_over(repo):
    script = HandoffScript([None])
    result, *_ = await run_pipeline(repo, script, name="n")
    assert result == "scaffold_complete" and "sbx_plan_assemble" not in script.names()


# ------------------------------------------------------------------ ENV-02: env_file_ref

# The analysis reads key names through this store (ENV-05), so the reference must resolve in it.
ENV_STORE = InMemoryEnvFileStore()
ENV_REF = ENV_STORE.put(b"APP_MODE=prod\n", file_id="envf-" + "a" * 32).model_dump(mode="json")


def _patches(history) -> set[str]:
    """Patch IDs in the history's `core_patch` markers (the payload encodings vary)."""
    return {
        name
        for e in history.events
        if e.HasField("marker_recorded_event_attributes")
        and e.marker_recorded_event_attributes.marker_name == "core_patch"
        for name in ("env-file-ref-v1", "analysis-child-v1", "sandbox-child-v1")
        if name.encode() in e.marker_recorded_event_attributes.SerializeToString()
    }


def _started_input(history):
    return json.loads(
        history.events[0].workflow_execution_started_event_attributes.input.payloads[0].data
    )


@pytest.mark.asyncio
async def test_the_env_file_ref_reaches_analysis_sandbox_and_planning_inputs(repo, monkeypatch):
    from cmp_worker.workflows.analysis import AnalysisWorkflow
    from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
    from temporalio.worker import Replayer

    fake = FakeRegister()
    monkeypatch.setattr(
        "cmp_worker.workers.provisioning.build_planning_register_activities", lambda _db: fake
    )
    script = HandoffScript([None])
    histories = {}
    planning = planning_settings()
    assert planning["env_file_ref"] != ENV_REF  # the start request's reference wins
    result, status, _, _, sandbox_input, _ = await run_pipeline(
        repo, script, name="e", planning=planning, env_file_ref=ENV_REF, histories=histories
    )
    assert result == "scaffold_complete"
    assert status["env_file_ref"] == ENV_REF
    assert _started_input(histories["analysis-migration-w"])["env_file_ref"] == ENV_REF
    assert sandbox_input["env_file_ref"] == ENV_REF
    assemble = next(p for n, p in script.calls if n == "sbx_plan_assemble")
    assert assemble["settings"]["env_file_ref"] == ENV_REF
    assert "env-file-ref-v1" in _patches(histories["migration-w"])
    for recorded in histories.values():
        await Replayer(
            workflows=[MigrationWorkflow, AnalysisWorkflow, SandboxVerificationWorkflow]
        ).replay_workflow(recorded)


@pytest.mark.asyncio
async def test_without_env_file_ref_no_input_changes_and_no_patch_marker(repo):
    script = Script([None])
    histories = {}
    _, status, _, _, sandbox_input, _ = await run_pipeline(
        repo, script, name="x", histories=histories
    )
    assert status["env_file_ref"] is None
    assert _started_input(histories["analysis-migration-w"]).get("env_file_ref") is None
    assert "env_file_ref" not in sandbox_input
    assert "env-file-ref-v1" not in _patches(histories["migration-w"])
    assert "analysis-child-v1" in _patches(histories["migration-w"])  # the helper reads markers


@pytest.mark.asyncio
async def test_the_api_scaffold_start_with_only_a_reference_keeps_the_scaffold_path():
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
                MigrationWorkflow.run,
                {"project_id": "project", "env_file_ref": ENV_REF},
                id="migration-ref",
                task_queue=WORKFLOW_QUEUE,
            )
            state = await until(handle, "status", lambda s: s["phase"] == "awaiting_approval")
            assert state["env_file_ref"] == ENV_REF and state["pipeline"] is None
            await handle.signal("approve")
            assert await asyncio.wait_for(handle.result(), timeout=30) == "scaffold_complete"
            child = await env.client.get_workflow_handle("analysis-migration-ref").fetch_history()
    assert _started_input(child) == "migration-ref"  # scaffold analysis input is unchanged
