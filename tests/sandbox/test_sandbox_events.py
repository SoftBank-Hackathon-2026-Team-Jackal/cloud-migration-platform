"""Provisioning 59: the Sandbox loop Activities emit `build.*`/`sandbox.*` for the web screen.

Fake service and builder; the in-memory emitter applies the same contract checks, masking and
idempotency rules as the database.
"""

import dataclasses
import json
from contextlib import nullcontext

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.events import DATA_MODELS
from cmp_contracts.provisioning import Image
from cmp_contracts.verification import canonical_requirements
from cmp_testsuite import Suite, dump_bundle
from cmp_testsuite.model import CaseResult, Expect, HttpStep, SuiteCase, SuiteResult
from cmp_worker.activities.sandbox_loop import SandboxLoopActivities
from cmp_worker.artifact_store import LocalArtifactStore
from cmp_worker.build_wiring import BuildFailed
from cmp_worker.events import configure_emitter
from cmp_worker.sandbox_deploy import SandboxError, SandboxPlan
from cmp_worker.sandbox_events import fix_lines, sbx_emit
from cmp_worker.sandbox_network import EgressRecord
from temporalio.testing import ActivityEnvironment

from .test_deploy import requirements

MIG = "migration-w"
DIGEST = "sha256:" + "e" * 64
IMAGE = Image(uri=f"registry.example.com/demo/app@{DIGEST}", platforms=["linux/amd64"])
PG = Image(uri="docker.io/library/postgres@sha256:" + "a" * 64, platforms=["linux/amd64"])
SECRET = "s3cr3t-db-password-value"


def ctx(run=1, attempt=1):
    return {"migration_id": MIG, "run": run, "attempt": attempt}


@pytest.fixture
def emitter():
    memory = InMemoryEmitter()
    previous = configure_emitter(memory)
    yield memory
    configure_emitter(previous)


def checked(emitter):
    """Every stored event validates against its contract model; nothing was rejected."""
    assert not [log for log in emitter.logs if log["code"].startswith("EVENT_")]
    for event in emitter.events:
        assert event.phase == "build" and event.migration_id == MIG
        DATA_MODELS[event.type].model_validate(event.data)
    return [(e.type, e.status, e.data) for e in emitter.events]


def steps(emitter):
    return [(e.data["step"], e.data["state"]) for e in emitter.events if e.type == "sandbox.step"]


def plan():
    return SandboxPlan(
        namespace="sbx-m1-r1", run_id="run1", expires_at=0, releases={}, secrets=[],
        request={"resources": []},
        network_intent=[{"external_host": "api.example.com", "port": 443}],
    )  # fmt: skip


class FakeService:
    """Only what the loop Activities call; `fail_at` raises a SandboxError there."""

    def __init__(self, *, fail_at=None, egress=(), expires_in=7200):
        self.fail_at = fail_at
        self.egress = list(egress)
        self.expires_in = expires_in
        self.calls = []

    def _call(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise SandboxError("HELM_FAILED")

    def prepare_sandbox(self, requirements, **kw):
        import time

        self._call("prepare_sandbox")
        return plan().model_copy(update={"expires_at": int(time.time()) + self.expires_in})

    def apply_pull_secret(self, *a):
        self._call("apply_pull_secret")

    def apply_sandbox(self, plan):
        self._call("apply_sandbox")

    def apply_network(self, plan, keep_hosts):
        self._call("apply_network")
        return []

    def check_sandbox(self, plan):
        self._call("check_sandbox")

    def snapshot_baseline(self, plan):
        self._call("snapshot_baseline")

    def pod_event_reasons(self, plan):
        return []

    def app_logs(self, plan):
        return f"DB_PASSWORD={SECRET}\n"

    def egress_records(self, plan):
        return self.egress

    def reset_test_data(self, plan):
        pass

    database_cut = nullcontext


def world(tmp_path, *, service=None, build=None, suite_result=None, cases=None, llm=None):
    store = LocalArtifactStore(tmp_path / "store")
    req_ref = store.put(
        canonical_requirements(requirements())[0], ref_id="requirements", kind="requirements"
    )
    cases = cases or [
        SuiteCase(
            id="std-health", source="STANDARD", effect="READ_ONLY",
            steps=[HttpStep(path="/health", expect=Expect(status=200))],
        ),
        SuiteCase(
            id="orig-write", source="ORIGINAL", effect="WRITES_DATA",
            steps=[HttpStep(method="POST", path="/orders", expect=Expect(status=201))],
        ),
        SuiteCase(
            id="ai-read", source="AI", effect="READ_ONLY",
            steps=[HttpStep(path="/orders", expect=Expect(status=200))],
        ),
    ]  # fmt: skip
    suite_ref = store.put(dump_bundle(Suite(name="s", cases=cases)), ref_id="suite", kind="t")
    result = suite_result or SuiteResult(
        suite_sha256="x", results=[CaseResult(id=c.id, status="PASSED") for c in cases]
    )
    activities = SandboxLoopActivities(
        service=service or FakeService(), store=store, workspace_root=tmp_path / "work",
        build=build or (lambda rid, src: IMAGE), suite_runner=lambda *_: result,
        llm_factory=llm,
    )  # fmt: skip
    data = {
        "migration_id": MIG, "run_id": "run1", "requirements_ref": req_ref.model_dump(mode="json"),
        "suite_ref": suite_ref.model_dump(mode="json"), "source": {"workspace": str(tmp_path)},
        "database_image": PG.model_dump(mode="json"),
        "keep_hosts": [{"host": "logs.example.com", "port": 443}],
    }  # fmt: skip
    activities.logs_ref = store.put(b"log", ref_id="logs", kind="sandbox-log").model_dump()
    return activities, data


# ---------------------------------------------------------------- sbx_build


@pytest.mark.asyncio
async def test_a_build_emits_its_check_and_a_succeeded_result(tmp_path, emitter):
    activities, data = world(tmp_path)
    payload = {
        "requirements_ref": data["requirements_ref"], "source": data["source"],
        "migration_id": MIG, "run_id": "run1", "attempt": 1, "events": ctx(),
    }  # fmt: skip
    await activities.sbx_build(payload)
    assert checked(emitter) == [
        ("build.check", "running", {"id": "build", "state": "running", "attempt": 1}),
        ("build.check", "running", {"id": "build", "state": "done", "attempt": 1}),
        ("build.result", "running", {
            "attempt": 1, "max_attempts": 3, "status": "SUCCEEDED", "digest": DIGEST,
            "image_uri": IMAGE.uri, "reused": False, "error_code": None,
        }),
    ]  # fmt: skip


class FailingBuilder:
    def __init__(self, code):
        self.code = code

    def __call__(self, *a):
        raise AssertionError("the evidence path is used")

    def build_with_evidence(self, *a, **kw):
        raise BuildFailed(self.code, "CODE")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "shown"), [("DOCKERFILE_MISSING", "DOCKERFILE_MISSING"), ("ODD", "BUILD_FAILED")]
)
async def test_a_failed_build_emits_a_failed_check_and_result_with_a_contract_code(
    tmp_path, emitter, code, shown
):
    activities, data = world(tmp_path, build=FailingBuilder(code))
    payload = {
        "requirements_ref": data["requirements_ref"], "source": data["source"],
        "migration_id": MIG, "run_id": "run1", "attempt": 2, "events": ctx(run=3, attempt=2),
    }  # fmt: skip
    out = await activities.sbx_build(payload)
    assert out["build_error"] == code
    events = checked(emitter)
    assert events[1] == ("build.check", "running", {"id": "build", "state": "failed", "attempt": 2})
    assert events[2][2]["status"] == "FAILED" and events[2][2]["error_code"] == shown


@pytest.mark.asyncio
async def test_without_an_events_context_nothing_is_emitted(tmp_path, emitter):
    activities, data = world(tmp_path)
    await activities.sbx_build(
        {"requirements_ref": data["requirements_ref"], "source": data["source"], "attempt": 1}
    )
    await activities.sbx_apply({"input": data, "plan": plan().model_dump(), "attempt": 1})
    assert emitter.events == []


# ---------------------------------------------------------------- sbx_prepare


@pytest.mark.asyncio
async def test_the_first_prepare_emits_the_sandbox_env_once(tmp_path, emitter):
    activities, data = world(tmp_path)
    payload = {
        "input": data, "requirements_ref": data["requirements_ref"],
        "images": {"orders-api": IMAGE.model_dump(mode="json")}, "events": ctx(),
    }  # fmt: skip
    await activities.sbx_prepare(payload)
    await activities.sbx_prepare({**payload, "previous_plan": plan().model_dump()})
    events = checked(emitter)
    assert len(events) == 1
    kind, status, env = events[0]
    assert kind == "sandbox.env" and status == "running"
    assert env["namespace"] == "sbx-m1-r1" and env["egress_allowed"] == 2
    assert 119 <= env["expires_minutes"] <= 120


# ---------------------------------------------------------------- sbx_apply


@pytest.mark.asyncio
async def test_apply_emits_render_apply_ready_in_order(tmp_path, emitter):
    activities, data = world(tmp_path)
    await activities.sbx_apply(
        {"input": data, "plan": plan().model_dump(), "attempt": 1, "events": ctx()}
    )
    assert steps(emitter) == [
        ("render", "running"), ("render", "done"), ("apply", "running"), ("apply", "done"),
        ("ready", "running"), ("ready", "done"),
    ]  # fmt: skip
    assert {e.data["run"] for e in emitter.events} == {1}
    checked(emitter)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_at", "last"), [("apply_sandbox", "apply"), ("check_sandbox", "ready")]
)
async def test_a_sandbox_error_marks_the_step_it_happened_in_as_failed(
    tmp_path, emitter, fail_at, last
):
    activities, data = world(tmp_path, service=FakeService(fail_at=fail_at))
    out = await activities.sbx_apply(
        {"input": data, "plan": plan().model_dump(), "attempt": 1, "events": ctx(run=2)}
    )
    assert out["sandbox_error"] == "HELM_FAILED"
    assert steps(emitter)[-2:] == [(last, "running"), (last, "failed")]
    checked(emitter)


@pytest.mark.asyncio
async def test_a_retried_apply_does_not_emit_its_events_again(tmp_path, emitter):
    activities, data = world(tmp_path)
    payload = {"input": data, "plan": plan().model_dump(), "attempt": 1, "events": ctx()}
    env = ActivityEnvironment()
    await env.run(activities.sbx_apply, payload)
    first = len(emitter.events)
    env.info = dataclasses.replace(env.info, attempt=2)  # Temporal runs the Activity again
    await env.run(activities.sbx_apply, payload)
    assert first == 6 and len(emitter.events) == first


# ---------------------------------------------------------------- sbx_verify


@pytest.mark.asyncio
async def test_a_passing_verify_emits_tests_test_and_judge_and_returns_checks(tmp_path, emitter):
    egress = [
        EgressRecord(decision="ALLOW", host="api.example.com", port=443, count=2),
        EgressRecord(decision="ALLOW", host="logs.example.com", port=443, count=1),
    ]
    activities, data = world(tmp_path, service=FakeService(egress=egress))
    applied = {"sandbox_error": None, "baseline": "SAVED"}
    out = await activities.sbx_verify(
        {"input": data, "plan": plan().model_dump(), "applied": applied, "source": data["source"],
         "suite_ref": data["suite_ref"], "events": ctx()}
    )  # fmt: skip
    events = checked(emitter)
    assert events[0] == (
        "sandbox.tests", "running",
        {"total": 3, "catalog": 1, "original": 1, "generated": 1, "read_only": 2},
    )  # fmt: skip
    assert steps(emitter) == [
        ("test", "running"), ("test", "done"), ("judge", "running"), ("judge", "done"),
    ]  # fmt: skip
    assert out["verdict"]["passed"] and out["checks"] == {
        "required_passed": 3, "required_total": 3, "unsupported_calls": 0,
        "calls_in_mapping": 2, "calls_total": 2, "blocked_egress": 0,
    }  # fmt: skip


@pytest.mark.asyncio
async def test_a_failed_judgement_fails_the_judge_step_and_counts_blocked_calls(tmp_path, emitter):
    result = SuiteResult(
        suite_sha256="x",
        results=[
            CaseResult(id="std-health", status="PASSED"),
            CaseResult(id="orig-write", status="FAILED", reason="STATUS_500"),
            CaseResult(id="ai-read", status="SKIPPED"),
        ],
    )
    egress = [EgressRecord(decision="BLOCK", host="s3.amazonaws.com", port=443, count=1)]
    activities, data = world(tmp_path, service=FakeService(egress=egress), suite_result=result)
    applied = {"sandbox_error": None}
    out = await activities.sbx_verify(
        {"input": data, "plan": plan().model_dump(), "applied": applied, "source": data["source"],
         "suite_ref": data["suite_ref"], "events": ctx(run=2, attempt=2)}
    )  # fmt: skip
    assert steps(emitter)[-2:] == [("judge", "running"), ("judge", "failed")]
    assert out["checks"]["required_passed"] == 1 and out["checks"]["required_total"] == 2
    assert out["checks"]["blocked_egress"] == 1 and out["checks"]["calls_in_mapping"] == 0
    checked(emitter)


@pytest.mark.asyncio
async def test_after_a_failed_apply_verify_emits_no_test_steps(tmp_path, emitter):
    activities, data = world(tmp_path)
    out = await activities.sbx_verify(
        {"input": data, "plan": plan().model_dump(), "applied": {"sandbox_error": "HELM_FAILED"},
         "source": data["source"], "suite_ref": data["suite_ref"], "events": ctx()}
    )  # fmt: skip
    assert emitter.events == [] and out["checks"]["required_total"] == 0


@pytest.mark.asyncio
async def test_no_secret_value_reaches_an_event(tmp_path, emitter):
    activities, data = world(tmp_path)
    await activities.sbx_verify(
        {"input": data, "plan": plan().model_dump(), "applied": {"sandbox_error": None},
         "source": data["source"], "suite_ref": data["suite_ref"], "events": ctx()}
    )  # fmt: skip
    assert emitter.events and SECRET not in json.dumps([e.data for e in emitter.events])


# ---------------------------------------------------------------- sbx_fix


@pytest.mark.asyncio
async def test_a_code_fix_emits_one_build_fix_per_file_with_secrets_masked(tmp_path, emitter):
    from cmp_agents.fixer import CodeFix, FileChange, FixOutcome

    (tmp_path / "app.py").write_text("HOST = '127.0.0.1'\nPORT = 80\n")
    fix = CodeFix(
        reason="bind on all interfaces",
        files=[
            FileChange(path="app.py", content="HOST = '0.0.0.0'\nPORT = 80\n"),
            FileChange(path="db.py", content=f'DB_PASSWORD = "{SECRET}"\n'),
        ],
    )
    activities, data = world(tmp_path, llm=lambda *_: None)
    import cmp_worker.activities.sandbox_loop as loop

    original = loop.propose_fix
    loop.propose_fix = lambda *a, **kw: FixOutcome(status="PROPOSED", code_fix=fix)
    try:
        out = await activities.sbx_fix(
            {"classification": {"failure_class": "CODE", "reasons": ["TEST_FAILED:std-health"]},
             "requirements_ref": data["requirements_ref"],
             "logs_ref": activities.logs_ref, "suite_ref": data["suite_ref"],
             "source": data["source"], "migration_id": MIG, "events": ctx()}
        )  # fmt: skip
    finally:
        loop.propose_fix = original
    events = checked(emitter)
    assert out["status"] == "PROPOSED" and out["fix_ids"] == ["fix-1-1", "fix-1-2"]
    assert [e[0] for e in events] == ["build.fix", "build.fix"]
    assert events[0][2] == {
        "id": "fix-1-1", "check": "code", "file": "app.py", "why": "TEST_FAILED:std-health",
        "evidence": "app.py",
        "lines": [{"kind": "del", "text": "- HOST = '127.0.0.1'"},
                  {"kind": "add", "text": "+ HOST = '0.0.0.0'"}],
    }  # fmt: skip
    assert events[1][2]["file"] == "db.py" and len(events[1][2]["lines"]) == 1
    assert SECRET not in json.dumps([e.data for e in emitter.events])


def test_an_env_file_line_shows_only_its_key(tmp_path):
    (tmp_path / ".env").write_text("DB_HOST=postgres\n")
    lines = fix_lines(str(tmp_path), ".env", f"DB_HOST=postgres.svc\nDB_PASSWORD={SECRET}\n")
    assert lines == [
        {"kind": "del", "text": "- DB_HOST"}, {"kind": "add", "text": "+ DB_HOST"},
        {"kind": "add", "text": "+ DB_PASSWORD"},
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_a_requirements_fix_emits_one_build_fix_per_changed_row(tmp_path, emitter):
    from cmp_agents.fixer import FixOutcome, RequirementsFix, ResourceFieldEdit

    activities, data = world(tmp_path, llm=lambda *_: None)
    import cmp_worker.activities.sandbox_loop as loop

    original = loop.propose_fix
    edit = ResourceFieldEdit(resource_id="orders-api", field="port", value=8081)
    fix = RequirementsFix(reason="port", edits=[edit])
    loop.propose_fix = lambda *a, **kw: FixOutcome(status="PROPOSED", requirements_fix=fix)
    try:
        out = await activities.sbx_fix(
            {"classification": {"failure_class": "SPEC", "reasons": ["TEST_FAILED:std-health"]},
             "requirements_ref": data["requirements_ref"],
             "logs_ref": activities.logs_ref, "suite_ref": data["suite_ref"],
             "source": data["source"], "migration_id": MIG, "events": ctx(run=2, attempt=1)}
        )  # fmt: skip
    finally:
        loop.propose_fix = original
    events = checked(emitter)
    assert out["kind"] == "REQUIREMENTS" and out["fix_ids"] == ["fix-1-1"]
    assert events == [("build.fix", "running", {
        "id": "fix-1-1", "check": "spec", "lines": [{"kind": "add", "text": "+ orders-api.port: 8081"}],
        "why": "TEST_FAILED:std-health", "evidence": "requirements",
    })]  # fmt: skip


# ---------------------------------------------------------------- sbx_emit


@pytest.mark.asyncio
async def test_the_emit_activity_stores_validated_events_once_across_retries(emitter):
    request = {
        "migration_id": MIG,
        "events": [
            {"type": "sandbox.done", "status": "running", "key": "sbx:1:1:done:PENDING",
             "data": {"status": "PASSED", "cleanup_status": "PENDING", "digest": DIGEST}},
        ],
    }  # fmt: skip
    env = ActivityEnvironment()
    assert await env.run(sbx_emit, request) == 1
    env.info = dataclasses.replace(env.info, attempt=2)
    await env.run(sbx_emit, request)
    assert len(checked(emitter)) == 1
