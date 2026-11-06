"""Ticket 29-B: a security loss goes through the same approval as a lost feature.

A `LOWER` judgement is an item `security:<criterion>` that a person must approve before the
Sandbox starts. Rejecting it blocks the move (a person who refuses a lower level does not turn a
"required" flag, as for a feature: there is none). `UNKNOWN`, `SAME`, `HIGHER` and a weak source
are shown but never asked for. Seam: the pure gate, then the analysis Workflow with fake activities.
"""

import asyncio
import contextlib
import os

import pytest
from cmp_contracts.analysis import AnalysisReport, Evidence, MappingRow, SecurityJudgement
from cmp_contracts.queues import ANALYSIS_QUEUE, WORKFLOW_QUEUE
from cmp_scanners.security_judge import PROOF
from cmp_worker.activities import analysis as real
from cmp_worker.review_gate import ReviewGate
from cmp_worker.workers.migration import create_workflow_worker
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

REF = {"id": "analysis", "version": "1"}
HTTPS = "security:EXTERNAL_HTTPS"
ADMIN = "security:ADMIN_API_EXPOSURE"


def judgement(criterion, verdict, *, source="PROTECTED", target="UNPROTECTED", weak=False):
    return SecurityJudgement(
        criterion=criterion,
        source_state=source,
        target_state=target,
        verdict=verdict,
        weak_source=weak,
        reasons=["TARGET_ENTRY_WITHOUT_TLS"] if verdict == "LOWER" else [],
        suggestion="ENFORCE_HTTPS_AT_ENTRY" if verdict == "LOWER" else None,
    ).model_dump(mode="json")


LOWER = judgement("EXTERNAL_HTTPS", "LOWER")
SAME_WEAK = judgement(
    "ADMIN_API_EXPOSURE", "SAME", source="UNPROTECTED", target="UNPROTECTED", weak=True
)
HIGHER = judgement("DB_ACCESS_SCOPE", "HIGHER", source="UNPROTECTED", target="PROTECTED", weak=True)
UNKNOWN = judgement("DB_TRANSPORT_ENCRYPTION", "UNKNOWN", source="UNKNOWN", target="UNPROTECTED")
ALL_FOUR = [LOWER, HIGHER, UNKNOWN, SAME_WEAK]


def row(id, grade):
    return MappingRow(
        id=id, capability="cap-" + id, usage="usage " + id, grade=grade,
        required_proposed=True, required_reason="why", required=None,
        disposition="MIGRATE", loses=["x"], treatment=None, status="EVIDENCE_CHECKED",
        evidence=[Evidence(kind="CODE", locator="a.go:1", verified=True)],
    )  # fmt: skip


def confirm(required, actor="alice"):
    return {"analysis_ref": REF, "required": required, "actor": actor}


def approve(approvals, actor="bob"):
    return {"analysis_ref": REF, "approvals": approvals, "actor": actor}


def codes(gate):
    return [r["code"] for r in gate.rejections]


# --- the pure gate ----------------------------------------------------------------------------


def test_a_lower_judgement_waits_for_approval_after_the_confirmation():
    gate = ReviewGate(REF, [row("a", "DIRECT")], security=ALL_FOUR)
    assert gate.confirm(confirm({"a": True}))
    assert gate.phase == "WAITING_APPROVAL" and gate.view()["pending"] == "approve_losses"


def test_only_lower_asks_for_an_approval():
    for judgements in ([HIGHER], [UNKNOWN], [SAME_WEAK], [HIGHER, UNKNOWN, SAME_WEAK]):
        gate = ReviewGate(REF, [row("a", "DIRECT")], security=judgements)
        gate.confirm(confirm({"a": True}))
        assert gate.phase == "APPROVED", judgements


def test_the_view_lists_every_judgement_and_marks_which_ones_need_an_answer():
    gate = ReviewGate(REF, [row("a", "DIRECT")], security=ALL_FOUR)
    items = {i["id"]: i for i in gate.view()["security"]}
    assert set(items) == {
        "security:EXTERNAL_HTTPS", "security:DB_ACCESS_SCOPE",
        "security:DB_TRANSPORT_ENCRYPTION", "security:ADMIN_API_EXPOSURE",
    }  # fmt: skip
    assert [i["approvable"] for i in items.values()] == [True, False, False, False]
    https = items[HTTPS]
    assert (https["verdict"], https["source_state"], https["target_state"]) == (
        "LOWER", "PROTECTED", "UNPROTECTED",
    )  # fmt: skip
    assert https["suggestion"] == "ENFORCE_HTTPS_AT_ENTRY" and https["decision"] is None
    assert items[ADMIN]["weak_source"] is True


def test_each_item_says_what_can_prove_it():
    items = {
        i["criterion"]: i["proof"]
        for i in ReviewGate(REF, [], security=ALL_FOUR).view()["security"]
    }
    assert items == {
        "EXTERNAL_HTTPS": "POST_DEPLOY",
        "DB_ACCESS_SCOPE": "SANDBOX",
        "DB_TRANSPORT_ENCRYPTION": "SANDBOX",
        "ADMIN_API_EXPOSURE": "POST_DEPLOY",
    }
    assert dict(PROOF) == items


def test_approving_the_lower_item_finishes_the_gate_and_keeps_the_decision():
    gate = ReviewGate(REF, [row("a", "DIRECT"), row("b", "PARTIAL")], security=ALL_FOUR)
    gate.confirm(confirm({"a": True, "b": False}))
    assert gate.approve(approve({"b": "APPROVED", HTTPS: "APPROVED"}))
    assert gate.phase == "APPROVED"
    decisions = gate.decisions()
    assert decisions["approvals"] == {"b": "APPROVED", HTTPS: "APPROVED"}
    assert decisions["required"] == {"a": True, "b": False}  # a security item has no such flag
    view = {i["id"]: i for i in gate.view()["security"]}
    assert view[HTTPS]["decision"] == "APPROVED"


def test_every_approvable_item_needs_an_answer_and_nothing_else_is_accepted():
    gate = ReviewGate(REF, [row("b", "PARTIAL")], security=ALL_FOUR)
    gate.confirm(confirm({"b": False}))
    assert not gate.approve(approve({"b": "APPROVED"}))  # the security item is missing
    assert not gate.approve(approve({"b": "APPROVED", HTTPS: "APPROVED", ADMIN: "APPROVED"}))
    assert not gate.approve(
        approve({"b": "APPROVED", HTTPS: "APPROVED", "security:NOPE": "APPROVED"})
    )
    assert codes(gate) == ["MISSING_ROW", "NOT_APPROVABLE", "UNKNOWN_ROW"]
    assert gate.phase == "WAITING_APPROVAL"


def test_rejecting_a_lower_item_blocks_with_the_reason_and_the_suggestion():
    gate = ReviewGate(REF, [row("a", "DIRECT")], security=ALL_FOUR)
    gate.confirm(confirm({"a": True}))
    assert gate.approve(approve({HTTPS: "REJECTED"}))
    assert gate.phase == "BLOCKED" and gate.finished
    (blocked,) = gate.blocked()
    assert blocked["id"] == HTTPS
    assert blocked["reason"] == "SECURITY_LOWER: EXTERNAL_HTTPS"
    assert blocked["alternative"] == "ENFORCE_HTTPS_AT_ENTRY"
    assert gate.decisions()["required"] == {"a": True}


def test_a_gate_with_only_security_still_asks_when_there_is_a_loss():
    gate = ReviewGate(REF, [], security=[LOWER])
    assert gate.phase == "WAITING_CONFIRMATION"
    assert gate.confirm(confirm({}))
    assert gate.phase == "WAITING_APPROVAL"
    assert gate.approve(approve({HTTPS: "APPROVED"})) and gate.phase == "APPROVED"


def test_a_gate_without_security_judgements_is_as_before():
    gate = ReviewGate(REF, [row("a", "DIRECT")])
    assert gate.view()["security"] == []
    gate.confirm(confirm({"a": True}))
    assert gate.phase == "APPROVED"


# --- the analysis Workflow --------------------------------------------------------------------


def env():
    return WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    )


def analysis_worker(client, rows, security):
    @activity.defn(name="check_evidence")
    async def check_evidence(payload: dict) -> dict:
        return AnalysisReport.model_validate(
            {
                "migration_id": payload["migration_id"],
                "ref": REF,
                "base_commit": "fixture",
                "commit_verified": False,
                "usage_profile": {},
                "mapping": rows,
                "precheck": {},
                "external_entries": [],
                "source_cidrs": [],
                "source_db_images": {},
                "unmapped_items": [],
                "unverified_risks": [],
                "security_judgements": security,
            }
        ).model_dump(mode="json")

    return Worker(
        client,
        task_queue=ANALYSIS_QUEUE,
        activities=[
            real.fetch_source, real.scan_code, real.scan_manifests, real.scan_aws_infra,
            real.scan_network, real.build_requirements, real.precheck, real.map_usage,
            check_evidence, real.record_review,
        ],
    )  # fmt: skip


@contextlib.asynccontextmanager
async def running(rows, security):
    async with await env() as e:  # noqa: SIM117
        async with analysis_worker(e.client, rows, security), create_workflow_worker(e.client):
            yield e


async def start(client):
    return await client.start_workflow(
        "AnalysisWorkflow", "mig", id="analysis-mig", task_queue=WORKFLOW_QUEUE
    )


async def until(handle, predicate, tries=200):
    for _ in range(tries):
        state = await handle.query("review_state")
        if predicate(state):
            return state
        await asyncio.sleep(0.02)
    pytest.fail("review state not reached")


@pytest.mark.asyncio
async def test_the_workflow_waits_for_the_security_approval_and_records_it():
    rows = [row("a", "DIRECT").model_dump(mode="json")]
    async with running(rows, ALL_FOUR) as e:
        handle = await start(e.client)
        state = await until(handle, lambda s: s["pending"] == "confirm_requirements")
        assert [i["id"] for i in state["security"]] == [
            "security:EXTERNAL_HTTPS", "security:DB_ACCESS_SCOPE",
            "security:DB_TRANSPORT_ENCRYPTION", "security:ADMIN_API_EXPOSURE",
        ]  # fmt: skip
        await handle.signal("confirm_requirements", confirm({"a": True}))
        await until(handle, lambda s: s["pending"] == "approve_losses")
        assert (await handle.describe()).status.name == "RUNNING"  # nothing approves it for you
        await handle.signal("approve_losses", approve({HTTPS: "APPROVED"}))
        result = await asyncio.wait_for(handle.result(), timeout=10)
    assert result["review"]["approvals"] == {HTTPS: "APPROVED"}
    assert result["review"]["required"] == {"a": True}


@pytest.mark.asyncio
async def test_rejecting_the_security_loss_stops_the_analysis_with_the_reason():
    rows = [row("a", "DIRECT").model_dump(mode="json")]
    async with running(rows, [LOWER]) as e:
        handle = await start(e.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        await handle.signal("confirm_requirements", confirm({"a": True}))
        await until(handle, lambda s: s["pending"] == "approve_losses")
        await handle.signal("approve_losses", approve({HTTPS: "REJECTED"}))
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(), timeout=10)
        assert failure.value.cause.type == "ANALYSIS_BLOCKED"
        state = await handle.query("review_state")
    assert state["phase"] == "BLOCKED"
    (blocked,) = state["blocked"]
    assert blocked["id"] == HTTPS and blocked["reason"] == "SECURITY_LOWER: EXTERNAL_HTTPS"
    assert blocked["alternative"] == "ENFORCE_HTTPS_AT_ENTRY"


@pytest.mark.asyncio
async def test_a_lower_judgement_stops_for_review_even_when_there_are_no_mapping_rows():
    async with running([], [LOWER]) as e:
        handle = await start(e.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        await handle.signal("confirm_requirements", confirm({}))
        await until(handle, lambda s: s["pending"] == "approve_losses")
        await handle.signal("approve_losses", approve({HTTPS: "APPROVED"}))
        result = await asyncio.wait_for(handle.result(), timeout=10)
    assert result["review"]["approvals"] == {HTTPS: "APPROVED"}


@pytest.mark.asyncio
async def test_nothing_lower_and_no_rows_needs_no_review_as_before():
    async with running([], [HIGHER, UNKNOWN]) as e:
        result = await asyncio.wait_for((await start(e.client)).result(), timeout=10)
    assert result["review"] is None
