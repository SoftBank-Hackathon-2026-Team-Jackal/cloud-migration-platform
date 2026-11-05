"""Integrated Sandbox loop: Workflow history with fake `sbx_*` Activities (ticket 14B)."""

import asyncio
import os

import pytest
from cmp_contracts.queues import SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker.workers.migration import create_workflow_worker
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

REF = {
    "id": "requirements",
    "version": "1",
    "kind": "requirements",
    "sha256": "a" * 64,
    "size_bytes": 1,
}
REF2 = {**REF, "sha256": "b" * 64}
SUITE = {**REF, "kind": "test-suite", "sha256": "c" * 64}
INPUT = {
    "migration_id": "mig", "run_id": "run1", "requirements_ref": REF, "suite_ref": SUITE,
    "source": {"workspace": "/ws"}, "database_image": {"uri": "x@sha256:" + "d" * 64, "platforms": ["linux/amd64"]},
}  # fmt: skip


class Script:
    """Fake Activities driven by a list of per-attempt outcomes; records every call."""

    def __init__(self, outcomes, *, fix=None, fix_status="PROPOSED", prepare_error=None):
        self.outcomes = list(outcomes)  # entries: None (pass) or a failure class
        self.fix = fix or {"kind": "REQUIREMENTS", "requirements_ref": REF2}
        self.fix_status = fix_status
        self.prepare_error = prepare_error
        self.calls: list[tuple[str, dict]] = []
        self.i = 0

    def record(self, name, payload):
        self.calls.append((name, payload))

    def names(self):
        return [n for n, _ in self.calls]

    def activities(self):
        script = self

        @activity.defn(name="sbx_build")
        async def build(payload: dict) -> dict:
            script.record("sbx_build", payload)
            return {
                "images": {"api": {"uri": "r@sha256:" + "e" * 64, "platforms": ["linux/amd64"]}}
            }

        @activity.defn(name="sbx_prepare")
        async def prepare(payload: dict) -> dict:
            script.record("sbx_prepare", payload)
            if script.prepare_error:
                return {"plan": None, "sandbox_error": script.prepare_error}
            n = len([c for c in script.calls if c[0] == "sbx_prepare"])
            return {"plan": {"n": n}, "sandbox_error": None}

        @activity.defn(name="sbx_apply")
        async def apply(payload: dict) -> dict:
            script.record("sbx_apply", payload)
            return {"sandbox_error": None, "baseline": "SAVED"}

        @activity.defn(name="sbx_verify")
        async def verify(payload: dict) -> dict:
            script.record("sbx_verify", payload)
            outcome = script.outcomes[min(script.i, len(script.outcomes) - 1)]
            script.i += 1
            reasons = [] if outcome is None else [f"TEST_FAILED:{outcome}"]
            return {
                "verdict": {"passed": outcome is None, "reasons": reasons}, "sandbox_error": None,
                "logs_ref": REF, "pod_events": [], "stack_in_app_code": False, "outcome": outcome,
            }  # fmt: skip

        @activity.defn(name="sbx_classify")
        async def classify(payload: dict) -> dict:
            script.record("sbx_classify", payload)
            klass = payload["verified"]["outcome"]
            return {
                "failure_class": klass,
                "reasons": [f"R:{klass}"],
                "fixer_allowed": klass not in {"INFRA", "RENDER"},
            }

        @activity.defn(name="sbx_fix")
        async def fix(payload: dict) -> dict:
            script.record("sbx_fix", payload)
            if script.fix_status != "PROPOSED":
                return {"status": script.fix_status, "reason": "LLM_NOT_CONFIGURED"}
            return {"status": "PROPOSED", "reason": "r", **script.fix}

        @activity.defn(name="sbx_teardown")
        async def teardown(payload: dict) -> dict:
            script.record("sbx_teardown", payload)
            return {"cleanup_status": "CLEANED"}

        return [build, prepare, apply, verify, classify, fix, teardown]


@workflow.defn
class ParentProbe:
    def __init__(self):
        self.pending: list[bool] = []

    @workflow.signal
    def sandbox_review_pending(self, pending: bool) -> None:
        self.pending.append(pending)

    @workflow.query
    def seen(self) -> list[bool]:
        return self.pending

    @workflow.run
    async def run(self) -> dict:
        child = await workflow.start_child_workflow(
            "SandboxVerificationWorkflow", INPUT, id="child-probe", task_queue=WORKFLOW_QUEUE
        )
        return await child


async def run(script, *, wait_review=False):
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow", INPUT, id="loop", task_queue=WORKFLOW_QUEUE
        )
        if wait_review:
            for _ in range(100):
                status = await handle.query("status")
                if status["phase"] == "NEEDS_REVIEW":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("review wait not reached")
            assert (await handle.describe()).status.name == "RUNNING"
            await handle.signal("resolve_review")
        return await asyncio.wait_for(handle.result(), timeout=10), await handle.query("status")


@pytest.mark.asyncio
async def test_a_spec_failure_is_fixed_by_a_new_requirements_version_without_a_build():
    script = Script(["SPEC", None])
    result, _ = await run(script)
    assert result["status"] == "PASSED" and result["requirements_ref"] == REF2
    assert script.names().count("sbx_build") == 1 and script.names().count("sbx_fix") == 1
    prepares = [p for n, p in script.calls if n == "sbx_prepare"]
    assert prepares[1]["requirements_ref"] == REF2 and prepares[1]["previous_plan"] == {"n": 1}
    applies = [p for n, p in script.calls if n == "sbx_apply"]
    assert applies[0]["previous_plan"] is None and applies[1]["previous_plan"] == {"n": 1}
    assert script.names()[-1] == "sbx_teardown"
    verifies = [p for n, p in script.calls if n == "sbx_verify"]
    assert [v["reset"] for v in verifies] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("klass", ["CODE", "BUILD"])
async def test_a_code_failure_builds_again_from_the_fixed_source(klass):
    script = Script([klass, None], fix={"kind": "CODE", "source": {"workspace": "/ws-fixed"}})
    result, _ = await run(script)
    assert result["status"] == "PASSED" and result["source"] == {"workspace": "/ws-fixed"}
    builds = [p for n, p in script.calls if n == "sbx_build"]
    assert [b["source"]["workspace"] for b in builds] == ["/ws", "/ws-fixed"]


@pytest.mark.asyncio
async def test_a_render_mismatch_stops_without_a_fix_and_still_cleans_up():
    script = Script(["RENDER"])
    result, _ = await run(script)
    assert result["status"] == "STOPPED_RENDER" and "sbx_fix" not in script.names()
    assert script.names()[-1] == "sbx_teardown"


@pytest.mark.asyncio
async def test_one_infra_failure_retries_with_the_same_input_and_does_not_use_an_attempt():
    script = Script(["INFRA", None])
    result, _ = await run(script)
    assert result["status"] == "PASSED" and "sbx_fix" not in script.names()
    assert script.names().count("sbx_apply") == 2
    assert [a["attempt"] for n, a in script.calls if n == "sbx_apply"] == [1, 1]


@pytest.mark.asyncio
async def test_two_infra_failures_in_a_row_wait_for_a_person_after_cleanup():
    script = Script(["INFRA", "INFRA"])
    result, _ = await run(script, wait_review=True)
    assert result["status"] == "NEEDS_REVIEW" and "sbx_fix" not in script.names()
    assert script.names().count("sbx_teardown") == 1


@pytest.mark.asyncio
async def test_three_failed_attempts_wait_for_a_person_and_record_each_attempt():
    script = Script(
        ["SPEC", "CODE", "SPEC"], fix={"kind": "REQUIREMENTS", "requirements_ref": REF2}
    )
    result, status = await run(script, wait_review=True)
    assert result["status"] == "NEEDS_REVIEW"
    assert [a["failure_class"] for a in result["attempts"]] == ["SPEC", "CODE", "SPEC"]
    assert script.names().count("sbx_fix") == 2 and script.names().count("sbx_teardown") == 1
    assert status["attempts"] == result["attempts"]


@pytest.mark.asyncio
async def test_a_fixer_that_gives_nothing_hands_the_run_to_a_person():
    script = Script(["SPEC"], fix_status="FAILED")
    result, _ = await run(script, wait_review=True)
    assert result["status"] == "NEEDS_REVIEW" and "FIXER:LLM_NOT_CONFIGURED" in result["reasons"]


@pytest.mark.asyncio
async def test_a_failed_prepare_is_cleaned_up_and_reported():
    script = Script([None], prepare_error="CLUSTER_COMMAND_FAILED")
    result, _ = await run(script, wait_review=True)
    assert result["status"] == "NEEDS_REVIEW" and result["reasons"] == [
        "SANDBOX:CLUSTER_COMMAND_FAILED"
    ]
    assert script.names().count("sbx_teardown") == 1 and "sbx_apply" not in script.names()


@pytest.mark.asyncio
async def test_the_parent_is_told_while_a_person_is_needed_and_the_terminal_state_is_failed():
    script = Script(["INFRA", "INFRA"])
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        create_workflow_worker(env.client),
        Worker(env.client, task_queue="probe", workflows=[ParentProbe]),
    ):
        parent = await env.client.start_workflow(ParentProbe.run, id="probe", task_queue="probe")
        child = env.client.get_workflow_handle("child-probe")
        for _ in range(100):
            try:
                if (await child.query("status"))["phase"] == "NEEDS_REVIEW":
                    break
            except Exception:  # noqa: BLE001 - the child may not exist yet
                await asyncio.sleep(0.05)
                continue
            await asyncio.sleep(0.05)
        for _ in range(100):
            if await parent.query("seen") == [True]:
                break
            await asyncio.sleep(0.05)
        assert await parent.query("seen") == [True]
        await child.signal("resolve_review")
        assert (await asyncio.wait_for(parent.result(), timeout=10))["status"] == "NEEDS_REVIEW"
        assert await parent.query("seen") == [True, False]
