"""Ticket 37: where the red-team step sits in the Sandbox loop's Workflow history.

Fake `sbx_*` Activities only. The red team's own behaviour is tested elsewhere (tickets 33-36); this
file pins the *connection*: when the step runs, what it is handed, how a Finding becomes a fix and a
re-check, and that a switched-off run is unchanged.
"""

import asyncio
import os

import pytest
from cmp_contracts.queues import SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker.workers.migration import create_workflow_worker
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from .test_fix_loop import INPUT, Script

RED = {
    "intensity": "PASSIVE",
    "time_budget_seconds": 300,
    "target_resource_id": "api",
    "consent_ref": "consent-1",
    "endpoints": [{"method": "GET", "path": "/items", "params": ["id"]}],
}
APPROVAL = {
    "approval_id": "ap-1",
    "approver": "park",
    "approved_at": "2026-11-07T00:00:00+00:00",
    "run_id": "run1",
    "intensity": "PASSIVE",
    "time_budget_seconds": 300,
}
FINDING = {
    "finding_id": "f-1",
    "category": "AUTHZ",
    "severity": "HIGH",
    "endpoint": "/items",
    "summary": "any user reads another user's item",
    "repro_summary": "GET /items?id=2 without a token",
    "repro": {"tool": "probe_get", "path": "/items?id=2"},
}


class RedScript(Script):
    """`reports` is one entry per red-team run: a list of open Findings (empty = clean) or a dict
    to return as is. A request always asks for `wait_seconds` of a person's time."""

    def __init__(self, *a, reports=None, wait_seconds=600, **kw):
        super().__init__(*a, **kw)
        self.reports = list(reports if reports is not None else [[]])
        self.wait_seconds = wait_seconds
        self.runs = 0

    def activities(self):
        script = self
        base = [
            a
            for a in super().activities()
            if getattr(a, "__temporal_activity_definition").name != "sbx_classify"
        ]

        @activity.defn(name="sbx_classify")
        async def classify(payload: dict) -> dict:
            script.record("sbx_classify", payload)
            if payload.get("open_findings"):
                return {
                    "failure_class": "SECURITY",
                    "reasons": [f"FINDING:{i}" for i in payload["open_findings"]],
                    "fixer_allowed": True,
                }
            klass = payload["verified"]["outcome"]
            return {
                "failure_class": klass,
                "reasons": [f"R:{klass}"],
                "fixer_allowed": klass not in {"INFRA", "RENDER"},
            }

        @activity.defn(name="sbx_redteam_request")
        async def request(payload: dict) -> dict:
            script.record("sbx_redteam_request", payload)
            return {
                "request": {"run_id": "run1", "intensity": "PASSIVE", "time_budget_seconds": 300},
                "wait_seconds": script.wait_seconds,
            }

        @activity.defn(name="sbx_redteam")
        async def redteam(payload: dict) -> dict:
            script.record("sbx_redteam", payload)
            report = script.reports[min(script.runs, len(script.reports) - 1)]
            script.runs += 1
            if isinstance(report, dict):
                return report
            previous = {f["finding_id"] for f in payload.get("previous_findings") or []}
            resolved = sorted(previous - {f["finding_id"] for f in report})
            return {
                "status": "DONE",
                "code": None,
                "open": report,
                "resolved": resolved,
                "summary": {"count": len(report)},
                "stopped_reason": "DONE",
            }

        return [*base, classify, request, redteam]


async def run(script, data, *, approve=True, decline=False):
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow", data, id="red-loop", task_queue=WORKFLOW_QUEUE
        )
        # Polling (not awaiting the result) keeps the test server from skipping time past the
        # approval wait while we are still deciding what to answer.
        answered = 0
        for _ in range(400):
            if (await handle.describe()).status.name != "RUNNING":
                break
            state = await handle.query("redteam_state")
            if state["phase"] == "WAITING_FOR_APPROVAL" and state["waiting_since"] != answered:
                answered = state["waiting_since"]
                if decline:
                    await handle.signal("decline_redteam")
                elif approve:
                    await handle.signal("approve_redteam", APPROVAL)
            elif state["phase"] == "WAITING_FOR_APPROVAL" and not approve and not decline:
                break  # nobody answers: let the clock run out
            await asyncio.sleep(0.02)
        return await asyncio.wait_for(handle.result(), timeout=20), await handle.query("status")


@pytest.mark.asyncio
async def test_with_the_switch_off_no_red_team_step_runs_and_nothing_changes():
    script = RedScript([None])
    result, _ = await run(script, INPUT)
    assert not [n for n in script.names() if n.startswith("sbx_redteam")]
    assert "redteam" not in result and result["status"] == "PASSED"


@pytest.mark.asyncio
async def test_a_clean_run_asks_a_person_then_attacks_once_in_the_passed_sandbox_before_publishing():
    script = RedScript([None], reports=[[]])
    result, _ = await run(script, {**INPUT, "redteam": RED})
    names = script.names()
    assert names.index("sbx_verify") < names.index("sbx_redteam_request")
    assert names.index("sbx_redteam_request") < names.index("sbx_redteam")
    assert (
        names.index("sbx_redteam") < names.index("sbx_teardown") < names.index("sbx_plan_publish")
    )
    assert names.count("sbx_redteam") == 1 and names.count("sbx_fix") == 0
    run_call = next(p for n, p in script.calls if n == "sbx_redteam")
    assert run_call["approval"] == APPROVAL and run_call["previous_findings"] == []
    assert run_call["input"]["redteam"] == RED and run_call["digest"] == "sha256:" + "e" * 64
    assert result["status"] == "PASSED" and result["redteam"]["status"] == "CLEAN"


@pytest.mark.asyncio
async def test_a_declined_request_skips_the_attack_and_the_run_still_passes():
    script = RedScript([None])
    result, _ = await run(script, {**INPUT, "redteam": RED}, decline=True)
    assert "sbx_redteam" not in script.names()
    assert result["status"] == "PASSED"
    assert result["redteam"]["status"] == "SKIPPED"
    assert result["redteam"]["code"] == "REDTEAM_DECLINED" and result["redteam"]["open"] == []


@pytest.mark.asyncio
async def test_no_answer_in_time_skips_the_attack_and_the_run_still_passes():
    script = RedScript([None], wait_seconds=30)
    result, _ = await run(script, {**INPUT, "redteam": RED}, approve=False)
    assert "sbx_redteam" not in script.names()
    assert result["redteam"]["code"] == "REDTEAM_APPROVAL_TIMEOUT" and result["status"] == "PASSED"


@pytest.mark.asyncio
async def test_a_refused_start_is_recorded_and_does_not_fail_the_run():
    refused = {
        "status": "SKIPPED",
        "code": "REDTEAM_ISOLATION_NOT_ENFORCED",
        "open": [],
        "resolved": [],
    }
    script = RedScript([None], reports=[refused])
    result, _ = await run(script, {**INPUT, "redteam": RED})
    assert result["status"] == "PASSED"
    assert result["redteam"]["status"] == "SKIPPED"
    assert result["redteam"]["code"] == "REDTEAM_ISOLATION_NOT_ENFORCED"


@pytest.mark.asyncio
async def test_an_open_finding_goes_through_the_fixer_a_new_build_and_only_its_own_re_check():
    script = RedScript(
        [None],
        reports=[[FINDING], []],
        fix={"kind": "CODE", "source": {"workspace": "/ws-fixed"}},
    )
    result, _ = await run(script, {**INPUT, "redteam": RED})
    classify = [p for n, p in script.calls if n == "sbx_classify"]
    assert classify[0]["open_findings"] == ["f-1"]
    fix = next(p for n, p in script.calls if n == "sbx_fix")
    assert fix["classification"]["failure_class"] == "SECURITY"
    assert [f["finding_id"] for f in fix["findings"]] == ["f-1"]
    assert "repro" not in fix["findings"][0]  # the fixer reads a summary, not the attack request
    assert script.names().count("sbx_build") == 2  # the fix is built again
    runs = [p for n, p in script.calls if n == "sbx_redteam"]
    assert len(runs) == 2
    assert runs[0]["previous_findings"] == []
    assert [f["finding_id"] for f in runs[1]["previous_findings"]] == ["f-1"]
    assert runs[1]["digest"] == runs[0]["digest"]  # fake builds give the same digest here
    assert script.names().count("sbx_redteam_request") == 2  # every run needs its own yes
    assert result["status"] == "PASSED" and result["source"] == {"workspace": "/ws-fixed"}
    assert result["redteam"]["status"] == "FIXED" and result["redteam"]["open"] == []


@pytest.mark.asyncio
async def test_a_finding_that_survives_every_fix_is_shown_and_does_not_block_the_run():
    script = RedScript(
        [None],
        reports=[[FINDING]],
        fix={"kind": "CODE", "source": {"workspace": "/ws-fixed"}},
    )
    result, _ = await run(script, {**INPUT, "redteam": RED})
    assert script.names().count("sbx_fix") == 3  # MAX_SECURITY_FIX_ATTEMPTS
    assert script.names().count("sbx_redteam") == 4  # the first attack and three re-checks
    assert result["status"] == "PASSED"  # R3: shown and recorded, not a gate
    assert result["redteam"]["status"] == "STILL_OPEN"
    assert [f["finding_id"] for f in result["redteam"]["open"]] == ["f-1"]
    assert "sbx_publish" in script.names()


@pytest.mark.asyncio
async def test_security_fixes_do_not_use_up_the_functional_fix_budget():
    # Three security rounds, then the same Sandbox fails a functional test: it still gets fixed.
    script = RedScript(
        [None, None, None, "SPEC", None],
        reports=[[FINDING], [FINDING], [FINDING], []],
        fix={"kind": "CODE", "source": {"workspace": "/ws-fixed"}},
    )
    result, _ = await run(script, {**INPUT, "redteam": RED})
    classes = [p["classification"]["failure_class"] for n, p in script.calls if n == "sbx_fix"]
    assert classes == ["SECURITY", "SECURITY", "SECURITY", "SPEC"]
    assert result["status"] == "PASSED"


@pytest.mark.asyncio
async def test_a_fix_that_lowers_a_protection_sends_the_run_to_a_person():
    script = RedScript([None], reports=[[FINDING]], fix_status="FAILED")
    script.fix_status = "FAILED"
    result, _ = await run_to_review(script)
    assert result == "NEEDS_REVIEW"
    assert "sbx_publish" not in script.names()


async def run_to_review(script):
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=script.activities()),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow",
            {**INPUT, "redteam": RED},
            id="red-review",
            task_queue=WORKFLOW_QUEUE,
        )
        for _ in range(200):
            state = await handle.query("redteam_state")
            status = await handle.query("status")
            if state["phase"] == "WAITING_FOR_APPROVAL":
                await handle.signal("approve_redteam", APPROVAL)
            if status["phase"] == "NEEDS_REVIEW":
                await handle.signal("resolve_review")
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("review wait not reached")
        result = await asyncio.wait_for(handle.result(), timeout=10)
        return result["status"], result


@pytest.mark.asyncio
async def test_a_sandbox_too_close_to_expiry_is_not_asked_about_at_all():
    script = RedScript([None], wait_seconds=0)
    result, _ = await run(script, {**INPUT, "redteam": RED}, approve=False)
    assert "sbx_redteam" not in script.names()
    assert result["redteam"]["code"] == "REDTEAM_TIME_INSUFFICIENT" and result["status"] == "PASSED"


def test_the_input_types_the_red_team_request():
    from cmp_worker.activities.sandbox_loop import SandboxInput

    parsed = SandboxInput.model_validate({**INPUT, "redteam": RED})
    assert parsed.redteam is not None and parsed.redteam.intensity.value == "PASSIVE"
    assert SandboxInput.model_validate(INPUT).redteam is None
    with pytest.raises(ValueError):
        SandboxInput.model_validate({**INPUT, "redteam": {**RED, "target_host": "10.0.0.1"}})
    with pytest.raises(ValueError):  # switching it on needs the consent reference
        SandboxInput.model_validate(
            {**INPUT, "redteam": {k: v for k, v in RED.items() if k != "consent_ref"}}
        )
