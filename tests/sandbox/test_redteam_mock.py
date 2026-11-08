"""Demo mock of the red-team attack and its security fix (`SandboxSettings.redteam_mock`).

The Activities replay a recorded run instead of calling a model; the Workflow is unchanged. The
recordings used here are small stand-ins in `tmp_path` so each rule is visible; the shipped
recording (`cmp_worker/redteam_mock/`) is checked on its own at the end.
"""

import asyncio
import hashlib
import json
import os

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.events import DATA_MODELS
from cmp_contracts.queues import SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker import redteam_mock
from cmp_worker.activities.sandbox_loop import SandboxLoopActivities
from cmp_worker.artifact_store import ArtifactRef, LocalArtifactStore
from cmp_worker.events import configure_emitter
from cmp_worker.redteam_safeguards import Finding
from cmp_worker.sandbox_input import SandboxSettings
from cmp_worker.workers.migration import create_workflow_worker
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from .test_deploy import requirements
from .test_fix_loop import INPUT
from .test_redteam_workflow import RED, RedScript
from .test_sandbox_events import FakeService

DIGEST = "sha256:" + "f" * 64
OLD_DIGEST = "sha256:" + "0" * 64
SUITE_SHA = "c" * 64
BEFORE = 'rows, _ := db.Query("SELECT * FROM items WHERE id = " + id)\n'
AFTER = 'rows, _ := db.Query("SELECT * FROM items WHERE id = $1", id)\n'


def recorded_finding(fid, category="sql injection"):
    return {
        "finding_id": fid, "category": category, "severity": "HIGH", "endpoint": "/items",
        "summary": "id is put into the query text", "repro_summary": "GET /items?id=1 OR 1=1",
        "evidence_ref": {"id": "redteam-evidence-x", "version": "1", "kind": "redteam-evidence",
                         "sha256": "9" * 64, "size_bytes": 3},
        "image_digest": OLD_DIGEST, "suite_sha256": "8" * 64, "intensity": "PASSIVE",
        "executed_at": "2026-11-08T01:00:00Z",
        "repro": {"tool": "probe_get", "path": "/items?id=1%20OR%201=1"},
    }  # fmt: skip


def write_recording(root, *, before=BEFORE):
    root.mkdir(parents=True, exist_ok=True)
    findings = [recorded_finding("sql-1"), recorded_finding("info-1", "information disclosure")]
    (root / "round1.json").write_text(
        json.dumps(
            {
                "result": {"status": "DONE", "open": findings, "stopped_reason": "DONE"},
                "evidence": {"sql-1": "HTTP 200 ... 42 rows"},
            }
        )
    )
    (root / "fix.json").write_text(
        json.dumps(
            {
                "reason": "use a bound parameter for id",
                "files": [
                    {
                        "path": "main.go",
                        "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                        "content": AFTER,
                    }
                ],
            }
        )
    )


@pytest.fixture
def emitter():
    memory = InMemoryEmitter()
    previous = configure_emitter(memory)
    yield memory
    configure_emitter(previous)


def loop(tmp_path, *, llm=None):
    store = LocalArtifactStore(tmp_path / "store")
    write_recording(tmp_path / "rec")
    return SandboxLoopActivities(
        service=FakeService(), store=store, workspace_root=tmp_path / "work", build=lambda *_: None,
        suite_runner=lambda *_: None, llm_factory=llm, redteam_mock_dir=tmp_path / "rec",
        redteam_mock_waits=(0.01, 0.0),
    )  # fmt: skip


APPROVAL = {
    "approval_id": "mig:run1:1", "approver": "consent-1", "approved_at": "2026-11-08T02:00:00Z",
    "run_id": "run1", "intensity": "PASSIVE", "time_budget_seconds": 300,
}  # fmt: skip


def red_payload(previous=(), mock=True):
    red = {**RED, **({"mock": True} if mock else {})}
    return {
        "input": {**INPUT, "redteam": red}, "plan": {"n": 1}, "requirements_ref": INPUT["requirements_ref"],
        "suite_ref": INPUT["suite_ref"], "approval": APPROVAL, "previous_findings": list(previous),
        "digest": DIGEST, "source_sha256": "s" * 64,
    }  # fmt: skip


def workspace(tmp_path, text=BEFORE):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "main.go").write_text(text)
    return ws


def fix_payload(ws, klass="SECURITY", events=None):
    return {
        "classification": {"failure_class": klass, "reasons": ["FINDING:sql-1"]},
        "requirements_ref": INPUT["requirements_ref"], "logs_ref": INPUT["requirements_ref"],
        "suite_ref": INPUT["suite_ref"], "source": {"workspace": str(ws)},
        "analysis_report_ref": None, "migration_id": "mig",
        **({"events": events} if events else {}),
    }  # fmt: skip


# ------------------------------------------------------------------ settings


def test_the_setting_is_off_by_default_and_reaches_the_red_team_block_only_when_on():
    assert SandboxSettings().redteam_mock is False
    on = SandboxSettings(redteam=True, redteam_consent_ref="c", redteam_mock=True)
    assert on.redteam_mock is True


# ------------------------------------------------------------------ sbx_redteam


async def test_the_first_mocked_attack_returns_the_recorded_findings_stamped_for_this_image(
    tmp_path,
):
    activities = loop(tmp_path)
    out = await activities.sbx_redteam(red_payload())
    assert out["status"] == "DONE" and out["code"] is None and out["resolved"] == []
    assert [f["finding_id"] for f in out["open"]] == ["sql-1", "info-1"]
    for f in out["open"]:
        Finding.model_validate({k: v for k, v in f.items() if k != "repro"})
        assert f["image_digest"] == DIGEST and f["suite_sha256"] == SUITE_SHA
        assert f["executed_at"] != "2026-11-08T01:00:00Z" and f["repro"]["tool"] == "probe_get"
    sql, info = out["open"]
    # Evidence is stored here again: the Finding never points at another server's artifact.
    assert activities.store.get(ArtifactRef.model_validate(sql["evidence_ref"])) == (
        b"HTTP 200 ... 42 rows"
    )
    assert info["evidence_ref"] is None
    assert out["summary"]["count"] == 2 and out["summary"]["finding_ids"] == ["sql-1", "info-1"]
    assert set(out) >= {"open", "resolved", "withheld", "summary", "stopped_reason"}


async def test_the_mocked_recheck_resolves_every_earlier_finding(tmp_path):
    activities = loop(tmp_path)
    first = await activities.sbx_redteam(red_payload())
    out = await activities.sbx_redteam(red_payload(previous=first["open"]))
    assert out["status"] == "DONE" and out["open"] == []
    assert out["resolved"] == ["sql-1", "info-1"] and out["summary"]["count"] == 0


async def test_a_mocked_attack_without_a_yes_attacks_nothing(tmp_path):
    activities = loop(tmp_path)
    out = await activities.sbx_redteam({**red_payload(), "approval": None})
    assert out["status"] == "SKIPPED" and out["code"] == "REDTEAM_NOT_APPROVED"


async def test_with_the_mock_off_the_real_stage_runs(tmp_path):
    activities = loop(tmp_path)  # no kit: the real stage says so and attacks nothing
    payload = red_payload(mock=False)
    payload["plan"] = {
        "namespace": "sbx", "run_id": "run1", "expires_at": 0, "releases": {}, "secrets": [],
        "request": {"resources": []}, "network_intent": [],
    }  # fmt: skip
    activities._requirements = lambda ref: requirements()
    out = await activities.sbx_redteam(payload)
    assert out["status"] == "SKIPPED" and out["code"] == "REDTEAM_NOT_CONFIGURED"
    assert not (tmp_path / "work" / "redteam-mock" / "mig").exists()


# ------------------------------------------------------------------ sbx_fix


async def test_a_mocked_security_fix_applies_the_recorded_change_and_shows_its_diff(
    tmp_path, emitter
):
    activities = loop(tmp_path)  # no model configured at all
    await activities.sbx_redteam(red_payload())
    ws = workspace(tmp_path)
    out = await activities.sbx_fix(
        fix_payload(ws, events={"migration_id": "mig", "run": 1, "attempt": 1})
    )
    assert out["status"] == "PROPOSED" and out["kind"] == "CODE"
    assert out["reason"] == "use a bound parameter for id" and out["changed"] == ["main.go"]
    assert out["fix_ids"] == ["fix-1-1"]
    fixed = tmp_path / "work" / out["source"]["workspace"].rsplit("/", 1)[-1]
    assert (fixed / "main.go").read_text() == AFTER
    assert (ws / "main.go").read_text() == BEFORE  # the original source is left as it was
    [event] = emitter.events
    assert event.type == "build.fix"
    DATA_MODELS[event.type].model_validate(event.data)
    assert event.data["check"] == "security" and event.data["file"] == "main.go"
    assert [line["kind"] for line in event.data["lines"]] == ["del", "add"]


async def test_a_recorded_fix_that_no_longer_fits_the_source_goes_to_the_real_fixer(tmp_path):
    activities = loop(tmp_path)  # the real Fixer here has no model: it says so
    await activities.sbx_redteam(red_payload())
    ws = workspace(tmp_path, text="package main // changed since the recording\n")
    out = await activities.sbx_fix(fix_payload(ws))
    assert out == {"status": "FAILED", "reason": "LLM_NOT_CONFIGURED"}


async def test_a_security_fix_of_a_run_that_was_not_mocked_goes_to_the_real_fixer(tmp_path):
    activities = loop(tmp_path)
    out = await activities.sbx_fix(fix_payload(workspace(tmp_path)))
    assert out == {"status": "FAILED", "reason": "LLM_NOT_CONFIGURED"}


async def test_other_failure_classes_always_go_to_the_real_fixer(tmp_path):
    activities = loop(tmp_path)
    await activities.sbx_redteam(red_payload())
    out = await activities.sbx_fix(fix_payload(workspace(tmp_path), klass="CODE"))
    assert out == {"status": "FAILED", "reason": "LLM_NOT_CONFIGURED"}


# ------------------------------------------------------------------ the Workflow, unchanged


async def test_with_the_mock_on_the_loop_finds_fixes_rechecks_and_passes(tmp_path, emitter):
    activities = loop(tmp_path)
    ws = workspace(tmp_path)
    script = RedScript([None])
    base = [
        a
        for a in script.activities()
        if getattr(a, "__temporal_activity_definition").name not in {"sbx_redteam", "sbx_fix"}
    ]

    @activity.defn(name="sbx_redteam")
    async def redteam(payload: dict) -> dict:
        script.record("sbx_redteam", payload)
        return await activities.sbx_redteam(payload)

    @activity.defn(name="sbx_fix")
    async def fix(payload: dict) -> dict:
        script.record("sbx_fix", payload)
        return await activities.sbx_fix(payload)

    data = {**INPUT, "source": {"workspace": str(ws)}, "redteam": {**RED, "mock": True}}
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=[*base, redteam, fix]),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow", data, id="red-mock", task_queue=WORKFLOW_QUEUE
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result["status"] == "PASSED" and result["redteam"]["status"] == "FIXED"
    assert result["redteam"]["resolved"] == ["sql-1", "info-1"]
    names = script.names()
    assert names.count("sbx_redteam") == 2 and names.count("sbx_fix") == 1
    fix_call = next(p for n, p in script.calls if n == "sbx_fix")
    assert fix_call["classification"]["failure_class"] == "SECURITY"
    rebuilt = [p for n, p in script.calls if n == "sbx_build"][-1]
    assert rebuilt["source"]["workspace"] != str(ws)
    phases = [
        e["data"]["phase"]
        for n, p in script.calls
        if n == "sbx_emit"
        for e in p["events"]
        if e["type"] == "sandbox.redteam"
    ]
    assert phases == ["RUNNING", "FIXING", "DONE"]
    assert [e.type for e in emitter.events] == ["build.fix"]


# ------------------------------------------------------------------ the shipped recording


def test_the_shipped_recording_is_a_valid_first_attack_and_fix():
    rec = redteam_mock.load("round1.json")
    fix = redteam_mock.load("fix.json")
    assert rec is not None and fix is not None, "recording of run 26c7a25b is missing"
    assert rec["source"].startswith("migration-26c7a25b")
    store_free = [f for f in rec["result"]["open"]]
    assert len(store_free) == 5
    categories = sorted(f["category"] for f in store_free)
    assert categories.count("sql injection") == 4 and "information disclosure" in categories
    for f in store_free:
        assert f["repro"]["tool"] and f["repro_summary"]
    assert fix["files"] and fix["reason"]
