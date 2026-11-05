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

AI_CASE = {
    "id": "ai-orders-roundtrip",
    "source": "AI",
    "effect": "READ_ONLY",
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


def build(tmp_path, *, llm, origin_ok=True, with_origin=True, fails_when_cut=True):
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
async def test_without_an_origin_runner_the_model_is_never_called(tmp_path):
    activities, payload, _, suite_ref = build(tmp_path, llm=ExplodingLLM(), with_origin=False)
    out = await activities.sbx_ai_tests(payload)
    assert out["status"] == "NOT_CALLED" and out["reason"] == "ORIGIN_UNAVAILABLE"
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
