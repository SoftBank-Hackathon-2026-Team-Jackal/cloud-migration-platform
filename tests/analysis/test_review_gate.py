import asyncio
import contextlib
import os
from datetime import timedelta

import pytest
from cmp_contracts.analysis import AnalysisReport, Evidence, MappingRow
from cmp_contracts.queues import ANALYSIS_QUEUE, WORKFLOW_QUEUE
from cmp_worker.activities import analysis as real
from cmp_worker.review_client import ReviewNotPendingError, get_review_state, send_review_signal
from cmp_worker.workers.migration import create_workflow_worker
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

REF = {"id": "analysis", "version": "1"}


def mapping_row(id, grade):
    return MappingRow(
        id=id, capability="cap-" + id, usage="usage " + id, grade=grade,
        required_proposed=True, required_reason="why", required=None,
        disposition="MIGRATE", loses=["x"], treatment=None, status="EVIDENCE_CHECKED",
        evidence=[Evidence(kind="CODE", locator="a.go:1", verified=True)],
    ).model_dump(mode="json")  # fmt: skip


ROWS = [mapping_row("a", "DIRECT"), mapping_row("b", "PARTIAL"), mapping_row("c", "NONE")]


def analysis_worker(client, rows):
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
async def running(rows):
    async with await env() as e:  # noqa: SIM117
        async with analysis_worker(e.client, rows), create_workflow_worker(e.client):
            yield e


def env():
    return WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    )


async def start(client, workflow_id="analysis-mig"):
    return await client.start_workflow(
        "AnalysisWorkflow", "mig", id=workflow_id, task_queue=WORKFLOW_QUEUE
    )


async def until(handle, predicate, tries=200):
    for _ in range(tries):
        state = await handle.query("review_state")
        if predicate(state):
            return state
        await asyncio.sleep(0.02)
    pytest.fail("review state not reached")


def confirm(required, ref=REF, actor="alice", **extra):
    return {"analysis_ref": ref, "required": required, "actor": actor, **extra}


def approve(approvals, ref=REF, actor="bob"):
    return {"analysis_ref": ref, "approvals": approvals, "actor": actor}


@pytest.mark.asyncio
async def test_workflow_waits_for_review_and_the_query_lists_items_without_treatments():
    async with running(ROWS) as e:
        handle = await start(e.client)
        state = await until(handle, lambda s: s["pending"] == "confirm_requirements")
        assert [i["id"] for i in state["items"]] == ["a", "b", "c"]
        assert all(i["treatment"] is None for i in state["items"])
        assert state["items"][1]["required_proposed"] is True
        assert (await handle.describe()).status.name == "RUNNING"


@pytest.mark.asyncio
async def test_confirm_then_approve_completes_with_an_immutable_review_record():
    async with running(ROWS) as e:
        handle = await start(e.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        await handle.signal(
            "confirm_requirements",
            confirm({"a": True, "b": False, "c": False}, operational=["backup"]),
        )
        state = await until(handle, lambda s: s["pending"] == "approve_losses")
        assert [i["treatment"] for i in state["items"]] == ["PROCEED", "APPROVAL", "APPROVAL"]
        await handle.signal("approve_losses", approve({"b": "APPROVED", "c": "APPROVED"}))
        result = await asyncio.wait_for(handle.result(), timeout=10)
    review = result["review"]
    assert review["analysis_ref"] == REF
    assert review["required"] == {"a": True, "b": False, "c": False}
    assert review["approvals"] == {"b": "APPROVED", "c": "APPROVED"}
    assert review["operational"] == ["backup"] and review["actor"] == "bob"
    assert review["decided_at"]
    assert [r["treatment"] for r in result["report"]["mapping"]] == [
        "PROCEED",
        "APPROVAL",
        "APPROVAL",
    ]


@pytest.mark.asyncio
async def test_a_blocking_confirmation_fails_the_child_with_reasons_and_alternatives():
    async with running(ROWS) as e:
        handle = await start(e.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        await handle.signal("confirm_requirements", confirm({"a": True, "b": False, "c": True}))
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(), timeout=10)
        assert failure.value.cause.type == "ANALYSIS_BLOCKED"
        state = await handle.query("review_state")
    assert state["phase"] == "BLOCKED"
    assert [b["id"] for b in state["blocked"]] == ["c"]
    assert state["blocked"][0]["alternative"]


@pytest.mark.asyncio
async def test_rejecting_a_loss_blocks_because_the_feature_becomes_required():
    async with running(ROWS) as e:
        handle = await start(e.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        await handle.signal("confirm_requirements", confirm({"a": True, "b": False, "c": False}))
        await until(handle, lambda s: s["pending"] == "approve_losses")
        await handle.signal("approve_losses", approve({"b": "REJECTED", "c": "APPROVED"}))
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(), timeout=10)
        assert failure.value.cause.type == "ANALYSIS_BLOCKED"
        assert [b["id"] for b in (await handle.query("review_state"))["blocked"]] == ["b"]


@pytest.mark.asyncio
async def test_wrong_version_signals_are_rejected_and_recorded_while_waiting_continues():
    async with running(ROWS) as e:
        handle = await start(e.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        other = {"id": "analysis", "version": "2"}
        await handle.signal(
            "confirm_requirements", confirm({"a": True, "b": True, "c": True}, ref=other)
        )
        state = await until(handle, lambda s: s["rejections"])
        assert [r["code"] for r in state["rejections"]] == ["ANALYSIS_REF_MISMATCH"]
        assert state["pending"] == "confirm_requirements"
        assert (await handle.describe()).status.name == "RUNNING"


@pytest.mark.asyncio
async def test_there_is_no_automatic_approval_however_long_it_waits():
    async with running(ROWS) as e:
        handle = await start(e.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        await e.sleep(timedelta(days=30))
        assert (await handle.describe()).status.name == "RUNNING"
        assert (await handle.query("review_state"))["pending"] == "confirm_requirements"


@pytest.mark.asyncio
async def test_signals_before_the_mapping_exists_are_rejected_and_nothing_is_lost():
    async with await env() as e:
        handle = await start(e.client)
        await handle.signal("confirm_requirements", confirm({"a": True}))
        async with analysis_worker(e.client, ROWS), create_workflow_worker(e.client):
            state = await until(handle, lambda s: s["pending"] == "confirm_requirements")
            assert [r["code"] for r in state["rejections"]] == ["NOT_READY"]


@pytest.mark.asyncio
async def test_an_analysis_without_mapping_rows_needs_no_review():
    async with running([]) as e:
        result = await asyncio.wait_for((await start(e.client)).result(), timeout=10)
    assert result["review"] is None


@pytest.mark.asyncio
async def test_the_cli_helper_only_signals_when_the_child_is_waiting_for_that_signal():
    async with running(ROWS) as e:
        handle = await start(e.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        assert (await get_review_state(e.client, "mig"))["pending"] == "confirm_requirements"
        with pytest.raises(ReviewNotPendingError):
            await send_review_signal(e.client, "mig", "approve", approve({"b": "APPROVED"}))
        await send_review_signal(
            e.client, "mig", "confirm", confirm({"a": True, "b": False, "c": False})
        )
        await until(handle, lambda s: s["pending"] == "approve_losses")
        with pytest.raises(ReviewNotPendingError):
            await send_review_signal(
                e.client, "other-migration", "approve", approve({"b": "APPROVED"})
            )
