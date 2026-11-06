"""Ticket 24 C: the AI test step in the Sandbox loop (fake model, fake runners)."""

from contextlib import contextmanager

import pytest
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_contracts.provisioning import Image
from cmp_contracts.verification import canonical_requirements
from cmp_testsuite import Suite, dump_bundle, load_bundle
from cmp_testsuite.model import CaseResult
from cmp_worker.activities.sandbox_loop import SandboxInput, SandboxLoopActivities
from cmp_worker.artifact_store import ArtifactRef, LocalArtifactStore

from .test_deploy import FakeRunner, make_service, requirements
from .test_publish import case
from .test_update import plan_with

PG = Image(uri="docker.io/library/postgres@sha256:" + "a" * 64, platforms=["linux/amd64"])

GO_SOURCE = (
    "package main\n\nfunc r(m *Mux) {\n"
    '\tm.HandleFunc("POST /api/messages", create)\n'
    '\tm.HandleFunc("GET /api/messages", list)\n'
    '\tm.HandleFunc("GET /orders", orders)\n}\n'
)
SHAPE = {
    "id": "messages",
    "write_path": "/api/messages",
    "write_body": {"client_msg_id": "std-{{run_id}}", "body": "value-{{run_id}}"},
    "write_status": 201,
    "id_from_response": "id",
    "value_field": "body",
    "read_back": "REPEAT_WRITE",
    "evidence": [{"file": "main.go", "line": 4}],
}
PROBE = {
    "id": "messages",
    "path": "/api/messages",
    "volatile": ["served_at"],
    "evidence": [{"file": "main.go", "line": 5}],
}
AI_CASE = {
    "id": "ai-orders-roundtrip",
    "source": "AI",
    "effect": "READ_ONLY",
    "evidence": [{"file": "main.go", "line": 6}],
    "steps": [
        {
            "action": "HTTP",
            "method": "GET",
            "path": "/orders",
            "expect": {"status": 200, "body_contains": "o-1"},
        }
    ],
}


class Switch:
    """The target passes while the dependency is there and fails while it is cut."""

    def __init__(self, fails_when_cut=True):
        self.cut = False
        self.fails_when_cut = fails_when_cut

    def runner(self):
        def run(c):
            failed = self.cut and self.fails_when_cut
            return CaseResult(id=c.id, status="FAILED" if failed else "PASSED")

        return run

    def cut_factory(self):
        @contextmanager
        def cutting():
            self.cut = True
            try:
                yield
            finally:
                self.cut = False

        return cutting


class ExplodingLLM:
    def __getattr__(self, name):
        raise AssertionError("the model must not be called")


def build(
    tmp_path, *, llm, origin_ok=True, with_origin=True, fails_when_cut=True, fetch=None,
    reset=None, order=None,
):  # fmt: skip
    (tmp_path / "main.go").write_text(GO_SOURCE)
    store = LocalArtifactStore(tmp_path / "store")
    reqs = requirements()
    req_ref = store.put(canonical_requirements(reqs)[0], ref_id="requirements", kind="requirements")
    suite = Suite(name="std", cases=[case("pg", ["postgres"])])
    suite_ref = store.put(dump_bundle(suite), ref_id="suite", kind="test-suite")
    data = SandboxInput(
        migration_id="mig-1", run_id="run1", requirements_ref=req_ref, suite_ref=suite_ref,
        source={"workspace": str(tmp_path)}, database_image=PG,
    )  # fmt: skip
    service = make_service(FakeRunner())
    plan = plan_with(service, tmp_path)
    switch = Switch(fails_when_cut)
    if order is not None:
        inner = switch.runner()

        def recording(c):
            order.append(c.id)
            return inner(c)

        switch.runner = lambda: recording
    origin = (
        (lambda: _ctx(lambda c: CaseResult(id=c.id, status="PASSED" if origin_ok else "FAILED")))
        if with_origin
        else None
    )
    activities = SandboxLoopActivities(
        service=service, store=store, workspace_root=tmp_path / "work", build=lambda *_: None,
        suite_runner=lambda *_: None, llm_factory=(lambda *_: llm) if llm is not None else None,
        target_case_runner=lambda p: _ctx(switch.runner()), origin_case_runner=origin,
        cut_dependency=lambda p: switch.cut_factory()(),
        target_fetcher=(lambda p: _ctx(fetch)) if fetch is not None else None,
        reset_data=reset,
    )  # fmt: skip
    payload = {
        "input": data.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
        "requirements_ref": req_ref.model_dump(), "suite_ref": suite_ref.model_dump(),
        "migration_id": "mig-1",
    }  # fmt: skip
    return activities, payload, store, suite_ref


@contextmanager
def _ctx(runner):
    yield runner


def scripted(*cases):
    return FakeLLMClient([LLMResponse(tool_calls=[call("submit_tests", cases=list(cases))])])


@pytest.mark.asyncio
async def test_without_an_origin_runner_a_shape_from_the_source_still_gives_tests(tmp_path):
    llm = FakeLLMClient([LLMResponse(tool_calls=[call("submit_tests", shapes=[SHAPE])])])
    activities, payload, store, _ = build(tmp_path, llm=llm, with_origin=False)
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "PINNED"
    assert out["accepted"] == ["ai-messages-roundtrip", "ai-messages-restart"]
    suite = load_bundle(store.get(ArtifactRef.model_validate(out["suite_ref"])))
    assert {c.id for c in suite.cases} == {"pg", "ai-messages-roundtrip", "ai-messages-restart"}


@pytest.mark.asyncio
async def test_the_tester_is_told_there_is_no_origin_and_sees_the_routes_of_the_source(tmp_path):
    llm = FakeLLMClient([LLMResponse(tool_calls=[call("submit_tests", shapes=[SHAPE])])])
    activities, payload, _, _ = build(tmp_path, llm=llm, with_origin=False)
    await activities.sbx_ai_tests(payload)
    sent = " ".join(m.content for m in llm.calls[0].messages)
    assert "POST /api/messages (main.go:4)" in sent and "GET /orders (main.go:6)" in sent
    assert "get_snippet" in {t.name for t in llm.calls[0].tools}


@pytest.mark.asyncio
async def test_a_shape_whose_citation_does_not_hold_is_rejected(tmp_path):
    wrong = {**SHAPE, "evidence": [{"file": "main.go", "line": 1}]}
    llm = FakeLLMClient([LLMResponse(tool_calls=[call("submit_tests", shapes=[wrong])])])
    activities, payload, _, suite_ref = build(tmp_path, llm=llm, with_origin=False)
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "NONE_ACCEPTED"
    assert set(out["rejected"].values()) == {"EVIDENCE_MISMATCH /api/messages"}
    assert out["suite_ref"] == suite_ref.model_dump(mode="json")


class Reads:
    """What the app answers to a GET: the same each time unless told otherwise."""

    def __init__(self, *bodies):
        self.bodies, self.asked = list(bodies), []

    def __call__(self, path):
        import json

        self.asked.append(path)
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return 200, json.dumps(body).encode()


def probes_llm():
    return FakeLLMClient([LLMResponse(tool_calls=[call("submit_tests", probes=[PROBE])])])


@pytest.mark.asyncio
async def test_a_probe_is_recorded_from_the_app_and_pinned_as_a_recorded_test(tmp_path):
    reads = Reads(
        {"items": [{"id": 1}], "served_at": "10:00"}, {"items": [{"id": 1}], "served_at": "10:01"}
    )
    resets = []
    activities, payload, store, _ = build(
        tmp_path, llm=probes_llm(), with_origin=False, fetch=reads, reset=resets.append
    )
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "PINNED" and out["accepted"] == ["ai-0rec-messages"]
    suite = load_bundle(store.get(ArtifactRef.model_validate(out["suite_ref"])))
    recorded = next(c for c in suite.cases if c.id == "ai-0rec-messages")
    assert recorded.basis == "RECORDED"
    assert recorded.steps[0].expect.json_equals == {"items.0.id": 1}
    assert reads.asked == ["/api/messages", "/api/messages"]
    assert len(resets) == 1  # the data goes back to the baseline the answers were recorded on


@pytest.mark.asyncio
async def test_a_probe_whose_answer_changes_between_calls_is_rejected_with_its_reason(tmp_path):
    reads = Reads({"n": 1, "served_at": "x"}, {"n": 2, "served_at": "x"})
    activities, payload, _, _ = build(
        tmp_path, llm=probes_llm(), with_origin=False, fetch=reads, reset=lambda p: None
    )
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "NONE_ACCEPTED"
    assert out["rejected"] == {"ai-0rec-messages": "RECORD_UNSTABLE n"}


@pytest.mark.asyncio
async def test_recorded_reads_are_checked_before_any_test_that_writes(tmp_path):
    llm = FakeLLMClient(
        [LLMResponse(tool_calls=[call("submit_tests", shapes=[SHAPE], probes=[PROBE])])]
    )
    order = []
    activities, payload, _, _ = build(
        tmp_path, llm=llm, with_origin=False, fetch=Reads({"n": 1}), reset=lambda p: None,
        order=order,
    )  # fmt: skip
    await activities.sbx_ai_tests(payload)
    assert order[0] == "ai-0rec-messages" and order.index("ai-messages-roundtrip") > 0


@pytest.mark.asyncio
async def test_when_the_data_cannot_go_back_to_the_baseline_recorded_tests_are_dropped(tmp_path):
    from cmp_worker.sandbox_deploy import SandboxError

    def broken(plan):
        raise SandboxError("BASELINE_MISSING")

    llm = FakeLLMClient(
        [LLMResponse(tool_calls=[call("submit_tests", shapes=[SHAPE], probes=[PROBE])])]
    )
    activities, payload, _, _ = build(
        tmp_path, llm=llm, with_origin=False, fetch=Reads({"n": 1}), reset=broken
    )
    out = await activities.sbx_ai_tests(payload)
    assert out["accepted"] == ["ai-messages-roundtrip", "ai-messages-restart"]
    assert out["rejected"] == {"ai-0rec-messages": "RESET_FAILED"}


@pytest.mark.asyncio
async def test_without_a_model_and_without_an_origin_nothing_is_called(tmp_path):
    activities, payload, _, suite_ref = build(tmp_path, llm=None, with_origin=False)
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "NOT_CALLED" and out["reason"] == "LLM_NOT_CONFIGURED"
    assert out["suite_ref"] == suite_ref.model_dump(mode="json")


@pytest.mark.asyncio
async def test_without_a_model_nothing_runs_and_the_suite_stays(tmp_path):
    activities, payload, _, suite_ref = build(tmp_path, llm=None)
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "NOT_CALLED" and out["reason"] == "LLM_NOT_CONFIGURED"
    assert out["suite_ref"] == suite_ref.model_dump(mode="json")


@pytest.mark.asyncio
async def test_a_test_that_meets_all_conditions_joins_a_new_pinned_suite(tmp_path):
    activities, payload, store, suite_ref = build(tmp_path, llm=scripted(AI_CASE))
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "PINNED" and out["accepted"] == ["ai-orders-roundtrip"]
    new_ref = ArtifactRef.model_validate(out["suite_ref"])
    assert new_ref.sha256 != suite_ref.sha256
    suite = load_bundle(store.get(new_ref))
    assert sorted(c.id for c in suite.cases) == ["ai-orders-roundtrip", "pg"]


@pytest.mark.asyncio
async def test_a_test_that_still_passes_without_the_dependency_is_rejected_and_the_suite_stays(
    tmp_path,
):
    activities, payload, _, suite_ref = build(tmp_path, llm=scripted(AI_CASE), fails_when_cut=False)
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "NONE_ACCEPTED" and out["accepted"] == []
    assert out["rejected"] == {"ai-orders-roundtrip": "PASSES_WITHOUT_DEPENDENCY"}
    assert out["suite_ref"] == suite_ref.model_dump(mode="json")


@pytest.mark.asyncio
async def test_a_test_that_fails_at_the_origin_is_rejected(tmp_path):
    activities, payload, _, _ = build(tmp_path, llm=scripted(AI_CASE), origin_ok=False)
    out = await activities.sbx_ai_tests(payload)
    assert out["rejected"] == {"ai-orders-roundtrip": "ORIGIN_FAILED"}
