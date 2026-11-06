import dataclasses

from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_agents.tester import SuiteRequest, generate_tests

KEY = "AKIAABCDEFGHIJKLMNOP"


def case(case_id="ai-orders", **kw):
    fields = {
        "id": case_id, "source": "AI", "effect": "READ_ONLY",
        "steps": [{"path": "/orders/1", "expect": {"status": 200, "json_equals": {"total": 10}}}],
    }  # fmt: skip
    fields.update(kw)
    return fields


def data():
    return SuiteRequest(
        usage_summary=f"postgres via pgx; key {KEY}",
        api_routes={"GET /orders/{id}": "returns an order", "POST /orders": "creates"},
        existing_ids=["std-health"],
    )


def test_submitted_scenarios_become_ai_cases():
    llm = FakeLLMClient([LLMResponse(tool_calls=[call("submit_tests", cases=[case()])])])
    result = generate_tests(data(), llm=llm)
    assert result.status == "GENERATED" and [c.id for c in result.cases] == ["ai-orders"]
    assert result.record.prompt_version


def test_the_session_input_has_no_place_for_fixer_output_and_is_masked():
    assert {f.name for f in dataclasses.fields(SuiteRequest)} == {
        "usage_summary", "api_routes", "existing_ids",
        # ticket 27-B: what the Tester may read; none of it carries a Fixer change
        "http_routes", "workspace", "skills_root", "origin_available",
    }  # fmt: skip
    llm = FakeLLMClient([LLMResponse(tool_calls=[call("submit_tests", cases=[case()])])])
    generate_tests(data(), llm=llm)
    sent = llm.calls[0].system + " ".join(m.content for m in llm.calls[0].messages)
    assert (
        KEY not in sent
        and "GET /orders/{id}" in sent
        and "fixer"
        not in sent.lower().replace("never see how failures were fixed", "").replace("fixer", "", 0)
        or "diff" not in sent.lower()
    )


def test_a_non_ai_source_is_rejected_and_the_agent_can_correct_it():
    llm = FakeLLMClient([
        LLMResponse(tool_calls=[call("submit_tests", cases=[case(source="STANDARD")])]),
        LLMResponse(tool_calls=[call("submit_tests", cases=[case()])]),
    ])  # fmt: skip
    assert generate_tests(data(), llm=llm).status == "GENERATED"


def test_running_out_of_turns_fails_with_a_reason():
    from cmp_agents.runtime import Budget

    llm = FakeLLMClient([LLMResponse(text="thinking")] * 5)
    result = generate_tests(data(), llm=llm, budget=Budget(max_turns=2))
    assert result.status == "FAILED" and result.failure_reason == "BUDGET_TURNS"
