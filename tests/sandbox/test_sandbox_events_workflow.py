"""Provisioning 59: the Sandbox Workflow hands each `sbx_*` Activity an events context and emits
`sandbox.attempt`, `sandbox.done` and `build.review` through `sbx_emit`, in the web mock's order."""

import json
import os
from pathlib import Path

import pytest
from cmp_contracts.events import DATA_MODELS
from cmp_contracts.queues import SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker.workers.migration import create_workflow_worker
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from .test_fix_loop import INPUT, Script, run

DIGEST = "sha256:" + "e" * 64
FIXTURE = Path(__file__).parent.parent / "fixtures" / "sandbox" / "sandbox-before-59-code-fix.json"


def timeline(script):
    """Activities with their (run, attempt) and Workflow-level events, in call order."""
    out = []
    for name, payload in script.calls:
        if name == "sbx_emit":
            assert payload["migration_id"] == INPUT["migration_id"]
            for event in payload["events"]:
                DATA_MODELS[event["type"]].model_validate(event["data"])
                out.append(_short(event))
        elif payload.get("events"):
            ctx = payload["events"]
            assert ctx["migration_id"] == INPUT["migration_id"]
            out.append((name, ctx["run"], ctx["attempt"]))
        else:
            out.append((name,))
    return out


def _short(event):
    data, kind = event["data"], event["type"]
    if kind == "sandbox.attempt":
        verdict = "passed" if data["verdict"]["passed"] else data["failure_class"]
        return (kind, data["run"], data["attempt"], verdict, tuple(data["fix_ids"]))
    if kind == "sandbox.done":
        return (kind, event["status"], data["status"], data["cleanup_status"], data["digest"])
    return (kind, event["status"], data.get("attempts"))


def emitted(script):
    return [e for n, p in script.calls if n == "sbx_emit" for e in p["events"]]


@pytest.mark.asyncio
async def test_a_pass_on_the_first_run_emits_one_attempt_and_a_passed_done():
    script = Script([None])
    result, _ = await run(script)
    assert result["status"] == "PASSED"
    assert timeline(script) == [
        ("sbx_build", 1, 1), ("sbx_prepare", 1, 1), ("sbx_apply", 1, 1), ("sbx_verify", 1, 1),
        ("sandbox.attempt", 1, 1, "passed", ()),
        ("sandbox.done", "running", "PASSED", "PENDING", DIGEST),
        ("sbx_teardown",),
        ("sandbox.done", "done", "PASSED", "CLEANED", DIGEST),
        ("sbx_plan_publish",), ("sbx_publish",),
    ]  # fmt: skip
    keys = [e["key"] for e in emitted(script)]
    assert keys == ["sbx:1:1:attempt:judged", "sbx:1:1:done:PENDING", "sbx:1:1:done:CLEANED"]


@pytest.mark.asyncio
async def test_a_code_failure_is_fixed_rebuilt_and_passes_on_the_second_attempt():
    fix = {"kind": "CODE", "source": {"workspace": "/ws-fixed"}, "fix_ids": ["fix-1-1"]}
    script = Script(["CODE", None], fix=fix)
    result, _ = await run(script)
    assert result["status"] == "PASSED"
    assert timeline(script) == [
        ("sbx_build", 1, 1), ("sbx_prepare", 1, 1), ("sbx_apply", 1, 1), ("sbx_verify", 1, 1),
        ("sbx_classify",),
        ("sandbox.attempt", 1, 1, "CODE", ()),
        ("sbx_fix", 1, 1),
        ("sandbox.attempt", 1, 1, "CODE", ("fix-1-1",)),
        ("sbx_build", 2, 2), ("sbx_prepare", 2, 2), ("sbx_apply", 2, 2), ("sbx_verify", 2, 2),
        ("sandbox.attempt", 2, 2, "passed", ()),
        ("sandbox.done", "running", "PASSED", "PENDING", DIGEST),
        ("sbx_teardown",),
        ("sandbox.done", "done", "PASSED", "CLEANED", DIGEST),
        ("sbx_plan_publish",), ("sbx_publish",),
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_an_infra_retry_starts_a_new_run_with_the_same_attempt():
    script = Script(["INFRA", None])
    await run(script)
    attempts = [e for e in timeline(script) if e[0] == "sandbox.attempt"]
    assert attempts == [
        ("sandbox.attempt", 1, 1, "INFRA", ()), ("sandbox.attempt", 2, 1, "passed", ()),
    ]  # fmt: skip
    assert ("sbx_apply", 2, 1) in timeline(script)


@pytest.mark.asyncio
async def test_three_failed_attempts_end_with_a_failed_done_and_a_review_wait():
    script = Script(["SPEC", "SPEC", "SPEC"])
    result, _ = await run(script, wait_review=True)
    assert result["status"] == "NEEDS_REVIEW"
    tail = timeline(script)[-6:]
    assert tail == [
        ("sbx_classify",),
        ("sandbox.attempt", 3, 3, "SPEC", ()),
        ("sandbox.done", "running", "NEEDS_REVIEW", "PENDING", None),
        ("sbx_teardown",),
        ("sandbox.done", "failed", "NEEDS_REVIEW", "CLEANED", None),
        ("build.review", "waiting", 3),
    ]  # fmt: skip
    review = emitted(script)[-1]
    assert review["data"] == {"reason": "max_attempts", "attempts": 3}


@pytest.mark.asyncio
async def test_a_render_stop_emits_a_failed_done_without_a_review():
    script = Script(["RENDER"])
    await run(script)
    kinds = [e["type"] for e in emitted(script)]
    assert kinds == ["sandbox.attempt", "sandbox.done", "sandbox.done"]
    assert emitted(script)[-1]["data"]["status"] == "STOPPED_RENDER"


class BuildErrorScript(Script):
    def activities(self):
        script = self
        base = [a for a in super().activities() if a.__name__ != "build"]

        @activity.defn(name="sbx_build")
        async def build(payload: dict) -> dict:
            script.record("sbx_build", payload)
            return {"images": None, "build_error": "DOCKERFILE_MISSING", "failure_class": "CODE"}

        return [*base, build]


@pytest.mark.asyncio
async def test_a_build_error_before_any_sandbox_shows_a_build_attempt_and_a_done():
    script = BuildErrorScript([None])
    result, _ = await run(script, wait_review=True)
    assert result["status"] == "NEEDS_REVIEW"
    assert timeline(script) == [
        ("sbx_build", 1, 1),
        ("sandbox.attempt", 1, 1, "BUILD", ()),
        ("sandbox.done", "failed", "NEEDS_REVIEW", "CLEANED", None),
    ]  # fmt: skip
    assert emitted(script)[0]["data"]["verdict"]["reasons"] == ["BUILD:DOCKERFILE_MISSING"]


@pytest.mark.asyncio
async def test_an_emit_failure_does_not_change_the_loop():
    class Broken(Script):
        def activities(self):
            keep = [a for a in super().activities() if a.__name__ != "emit"]

            @activity.defn(name="sbx_emit")
            async def emit(payload: dict) -> int:
                raise RuntimeError("store down")

            return [*keep, emit]

    script = Broken(["SPEC", None])
    result, _ = await run(script)
    assert result["status"] == "PASSED"


@pytest.mark.asyncio
async def test_a_history_from_before_this_ticket_replays_without_events():
    history = WorkflowHistory.from_json("loop", json.loads(FIXTURE.read_text()))
    await Replayer(workflows=[SandboxVerificationWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_a_new_history_replays():
    script = Script(["CODE", None], fix={"kind": "CODE", "source": {"workspace": "/w2"}})
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow", INPUT, id="loop-new", task_queue=WORKFLOW_QUEUE
        )
        await handle.result()
        history = await handle.fetch_history()
    await Replayer(workflows=[SandboxVerificationWorkflow]).replay_workflow(history)
