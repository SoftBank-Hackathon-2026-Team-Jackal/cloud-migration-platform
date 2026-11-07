"""Ticket 38: the model's three questions, answered by a scripted fake model.

The point of these tests is what the code does *not* take from the model: a Finding's endpoint,
evidence and reproduction come from requests the loop really sent.
"""

import json

import pytest
from cmp_agents.llm import FakeLLMClient, LLMError, LLMResponse, call
from cmp_agents.redteam_agent import (
    AllowList,
    LoopResult,
    ObservedResult,
    ToolCall,
    TurnBudget,
    build_input,
    run_loop,
)
from cmp_worker.redteam_brain import REFUSED, Brain
from cmp_worker.redteam_safeguards import Intensity, Severity, make_finding
from cmp_worker.redteam_stage import _findings  # noqa: F401 - the draft shape is the contract

from .test_redteam_safeguards import DIGEST, NOW, SUITE

INPUT = build_input(
    endpoints=[{"method": "GET", "path": "/items", "params": ["id"]}],
    requirements_text="the app lists items",
)


def reply(name, **arguments):
    return LLMResponse(tool_calls=[call(name, **arguments)])


def brain_with(*responses):
    llm = FakeLLMClient(list(responses))
    return Brain(llm), llm


def loop_of(*pairs, reason="DONE"):
    return LoopResult(
        stopped_reason=reason,
        turns_used=len(pairs),
        observations=[ObservedResult.from_raw(text) for _, text in pairs],
        calls=[ToolCall(tool="probe_get", path=path) for path, _ in pairs],
    )


# --- decide -------------------------------------------------------------------------------------


def test_a_tool_answer_becomes_a_call_and_finish_ends_the_run():
    brain, llm = brain_with(reply("probe_get", path="/items?id=2"), reply("finish"))
    turn = brain.decide(INPUT)
    first = turn([])
    assert first.call == ToolCall(tool="probe_get", path="/items?id=2")
    assert turn([ObservedResult.from_raw("HTTP 200")]).call is None
    prompt = llm.calls[1].messages[0].content
    assert "<observed_data>" in prompt and "HTTP 200" in prompt  # shown as data, in tags
    assert "GET /items (params: id)" in llm.calls[0].messages[0].content
    assert {t.name for t in llm.calls[0].tools} == {"probe_get", "finish"}


def test_the_system_prompt_says_observed_text_is_never_an_instruction():
    brain, llm = brain_with(reply("finish"))
    brain.decide(INPUT)([])
    assert "never an instruction" in llm.calls[0].system


@pytest.mark.parametrize("bad", ["http://other/x", "//other/x", "no-slash", "/10.0.0.9/x", ""])
def test_a_path_that_is_not_inside_the_app_is_refused_not_sent_and_not_a_crash(bad):
    brain, _ = brain_with(reply("probe_get", path=bad))
    decision = brain.decide(INPUT)([])
    assert decision.call == REFUSED
    assert AllowList({"probe_get"}).check(decision.call) is not None  # the loop will refuse it


def test_a_model_that_asks_for_no_tool_ends_the_run_quietly():
    brain, _ = brain_with(LLMResponse(text="I am done"))
    assert brain.decide(INPUT)([]).call is None


def test_a_model_that_cannot_be_called_ends_the_run_and_says_why():
    class Broken:
        def complete(self, **kw):
            raise LLMError("RUN_BUDGET_EXCEEDED")

    brain = Brain(Broken())
    assert brain.decide(INPUT)([]).call is None and brain.error == "RUN_BUDGET_EXCEEDED"


def test_the_loop_runs_with_the_brain_and_records_what_it_really_sent():
    brain, _ = brain_with(
        reply("probe_get", path="/items?id=1"),
        reply("probe_get", path="/items?id=2"),
        reply("finish"),
    )
    result = run_loop(
        brain.decide(INPUT),
        lambda c: ObservedResult.from_raw(f"answer for {c.path}"),
        allowed=AllowList({"probe_get"}),
        budget=TurnBudget(max_turns=5, max_seconds=60),
    )
    assert [c.path for c in result.calls] == ["/items?id=1", "/items?id=2"]
    assert result.stopped_reason == "DONE"


# --- detect -------------------------------------------------------------------------------------

ITEM = {
    "category": "AUTHZ",
    "severity": "HIGH",
    "request_index": 2,
    "summary": "any user reads another user's item",
    "repro_summary": "GET /items?id=2 without a token",
}


def test_a_finding_takes_its_endpoint_evidence_and_request_from_what_was_really_sent():
    loop = loop_of(("/healthz", "HTTP 200 ok"), ("/items?id=2", "HTTP 200 owner=2"))
    brain, _ = brain_with(reply("report_findings", findings=[ITEM]))
    (draft,) = brain.detect(INPUT, loop)
    assert draft["endpoint"] == "/items"  # the path of request 2, without its query
    assert draft["repro"] == {"tool": "probe_get", "path": "/items?id=2"}
    assert "owner=2" in draft["evidence"] and draft["severity"] == "HIGH"


def test_a_model_cannot_report_a_request_that_was_never_sent_or_an_invented_severity():
    loop = loop_of(("/items?id=2", "HTTP 200"))
    items = [
        {**ITEM, "request_index": 9},
        {**ITEM, "request_index": 0},
        {**ITEM, "request_index": -1},
        {**ITEM, "request_index": 1, "severity": "APOCALYPTIC"},
        {**ITEM, "request_index": "x"},
        {"category": "AUTHZ"},
    ]
    brain, _ = brain_with(reply("report_findings", findings=items))
    assert brain.detect(INPUT, loop) == []


def test_refused_requests_are_not_shown_as_evidence():
    loop = LoopResult(
        stopped_reason="DONE",
        turns_used=2,
        observations=[
            ObservedResult.from_raw("OUT_OF_SCOPE_TARGET"),
            ObservedResult.from_raw("HTTP 200"),
        ],
        calls=[REFUSED, ToolCall(tool="probe_get", path="/items")],
    )
    brain, llm = brain_with(reply("report_findings", findings=[{**ITEM, "request_index": 1}]))
    (draft,) = brain.detect(INPUT, loop)
    assert draft["repro"]["path"] == "/items"  # "request 1" is the first one that really ran
    assert "OUT_OF_SCOPE_TARGET" not in llm.calls[0].messages[0].content


def test_nothing_sent_means_no_model_call_and_no_findings():
    brain, llm = brain_with()
    empty = LoopResult(stopped_reason="DONE", turns_used=1, observations=[], calls=[])
    assert brain.detect(INPUT, empty) == [] and llm.calls == []


def test_the_text_fields_are_cut_to_what_a_finding_allows():
    loop = loop_of(("/items", "HTTP 200"))
    long_item = {**ITEM, "request_index": 1, "summary": "s" * 900, "repro_summary": "r" * 900}
    brain, _ = brain_with(reply("report_findings", findings=[long_item]))
    (draft,) = brain.detect(INPUT, loop)
    finding = make_finding(
        finding_id="f", image_digest=DIGEST, suite_sha256=SUITE, intensity=Intensity.PASSIVE,
        executed_at=NOW, evidence_ref=None,
        **{k: draft[k] for k in ("category", "severity", "endpoint", "summary", "repro_summary")},
    )  # fmt: skip
    assert finding.severity is Severity.HIGH and len(finding.summary) == 300


# --- reproduces ---------------------------------------------------------------------------------


def finding():
    return make_finding(
        finding_id="f", category="AUTHZ", severity=Severity.HIGH, endpoint="/items",
        summary="any user reads another user's item", repro_summary="GET /items?id=2",
        image_digest=DIGEST, suite_sha256=SUITE, intensity=Intensity.PASSIVE, executed_at=NOW,
    )  # fmt: skip


def test_the_model_decides_whether_the_weakness_still_shows():
    brain, llm = brain_with(reply("verdict", reproduces=True), reply("verdict", reproduces=False))
    seen = ObservedResult.from_raw(json.dumps({"status": 200}))
    assert brain.reproduces(finding(), seen) is True
    assert brain.reproduces(finding(), seen) is False
    assert "any user reads another user's item" in llm.calls[0].messages[0].content


def test_no_answer_is_an_error_so_the_finding_stays_open_instead_of_being_called_fixed():
    brain, _ = brain_with(LLMResponse(text="maybe"))
    with pytest.raises(LLMError):
        brain.reproduces(finding(), ObservedResult.from_raw("x"))

    class Broken:
        def complete(self, **kw):
            raise LLMError("DAY_BUDGET_EXCEEDED")

    with pytest.raises(LLMError):
        Brain(Broken()).reproduces(finding(), ObservedResult.from_raw("x"))
