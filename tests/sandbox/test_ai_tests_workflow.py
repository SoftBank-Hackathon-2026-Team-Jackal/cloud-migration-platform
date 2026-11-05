"""Ticket 24 C: where the AI test step sits in the Sandbox loop's Workflow history."""

import asyncio
import os

import pytest
from cmp_contracts.queues import SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker.workers.migration import create_workflow_worker
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from .test_fix_loop import INPUT, Script

NEW_SUITE = {**INPUT["suite_ref"], "sha256": "f" * 64}


class AiScript(Script):
    def __init__(self, *a, ai=None, **kw):
        super().__init__(*a, **kw)
        self.ai = ai or {
            "status": "PINNED",
            "suite_ref": NEW_SUITE,
            "accepted": ["ai-x"],
            "rejected": {},
        }

    def activities(self):
        script = self

        @activity.defn(name="sbx_ai_tests")
        async def ai_tests(payload: dict) -> dict:
            script.record("sbx_ai_tests", payload)
            return script.ai

        return [*super().activities(), ai_tests]


async def run(script, data):
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow", data, id="ai-loop", task_queue=WORKFLOW_QUEUE
        )
        return await handle.result()


@pytest.mark.asyncio
async def test_with_the_switch_off_no_ai_step_runs_and_nothing_changes():
    script = AiScript([None])
    result = await run(script, INPUT)
    assert "sbx_ai_tests" not in script.names() and "ai_tests" not in result
    verify = next(p for n, p in script.calls if n == "sbx_verify")
    assert verify["suite_ref"] == INPUT["suite_ref"]


@pytest.mark.asyncio
async def test_with_the_switch_on_the_step_runs_once_after_the_first_apply_and_the_new_suite_is_used():
    script = AiScript([None])
    result = await run(script, {**INPUT, "ai_tests": True})
    names = script.names()
    assert names.count("sbx_ai_tests") == 1
    assert names.index("sbx_apply") < names.index("sbx_ai_tests") < names.index("sbx_verify")
    verify = next(p for n, p in script.calls if n == "sbx_verify")
    assert verify["suite_ref"] == NEW_SUITE
    publish = next(p for n, p in script.calls if n == "sbx_plan_publish")
    assert publish["input"]["suite_ref"] == NEW_SUITE
    assert result["ai_tests"]["status"] == "PINNED" and result["ai_tests"]["accepted"] == ["ai-x"]


@pytest.mark.asyncio
async def test_a_second_attempt_does_not_ask_again_and_keeps_the_pinned_suite():
    script = AiScript(["SPEC", None])
    await run(script, {**INPUT, "ai_tests": True})
    assert script.names().count("sbx_ai_tests") == 1
    verifies = [p for n, p in script.calls if n == "sbx_verify"]
    assert [v["suite_ref"] for v in verifies] == [NEW_SUITE, NEW_SUITE]


@pytest.mark.asyncio
async def test_a_skipped_step_leaves_the_suite_and_records_why():
    skipped = {
        "status": "NOT_CALLED",
        "reason": "ORIGIN_UNAVAILABLE",
        "suite_ref": INPUT["suite_ref"],
        "accepted": [],
        "rejected": {},
    }
    script = AiScript([None], ai=skipped)
    result = await run(script, {**INPUT, "ai_tests": True})
    verify = next(p for n, p in script.calls if n == "sbx_verify")
    assert verify["suite_ref"] == INPUT["suite_ref"]
    assert result["ai_tests"]["reason"] == "ORIGIN_UNAVAILABLE"


class BuildScript(AiScript):
    def __init__(self, *a, build_error=None, **kw):
        super().__init__(*a, **kw)
        self.build_error = build_error

    def activities(self):
        script = self
        base = super().activities()

        @activity.defn(name="sbx_build")
        async def build(payload: dict) -> dict:
            script.record("sbx_build", payload)
            if script.build_error:
                return {"images": None, "build_error": script.build_error, "failure_class": "BUILD"}
            return {
                "source_sha256": "s" * 64,
                "images": {"api": {"uri": "r@sha256:" + "e" * 64, "platforms": ["linux/amd64"]}},
                "builds_ref": {**INPUT["suite_ref"], "kind": "build-attempts"},
            }

        return [
            a for a in base if getattr(a, "__temporal_activity_definition").name != "sbx_build"
        ] + [build]


@pytest.mark.asyncio
async def test_a_build_that_gives_no_image_waits_for_a_person_without_a_sandbox():
    script = BuildScript([None], build_error="DOCKERFILE_MISSING")
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow", INPUT, id="build-fail", task_queue=WORKFLOW_QUEUE
        )
        for _ in range(100):
            status = await handle.query("status")
            if status["phase"] == "NEEDS_REVIEW":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("review wait not reached")
        await handle.signal("resolve_review")
        result = await asyncio.wait_for(handle.result(), timeout=10)
    assert result["status"] == "NEEDS_REVIEW" and result["reasons"] == ["BUILD:DOCKERFILE_MISSING"]
    assert "sbx_prepare" not in script.names() and "sbx_teardown" not in script.names()


@pytest.mark.asyncio
async def test_the_build_evidence_reference_comes_back_in_the_result():
    script = BuildScript([None])
    result = await run(script, INPUT)
    assert result["builds_ref"]["kind"] == "build-attempts"
    first_build = next(p for n, p in script.calls if n == "sbx_build")
    assert first_build["attempt"] == 1 and first_build["run_id"] == "run1"
