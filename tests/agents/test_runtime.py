from pathlib import Path

import pytest
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_agents.prompt_store import load_prompt
from cmp_agents.runtime import Budget, run_agent
from cmp_agents.skills import SkillLoader, SkillNotFoundError, make_load_skill_tool
from cmp_agents.tools import ForbiddenToolError, ToolRegistry, ToolSpec
from pydantic import BaseModel

REPO_SKILLS = Path(__file__).parent.parent.parent / "skills"
FAKE_AWS_KEY = "AKIAABCDEFGHIJKLMNOP"


class Lookup(BaseModel):
    key: str


class Mapping(BaseModel):
    capability: str
    grade: str


def lookup_tool(result="found it"):
    return ToolSpec(
        name="get_evidence",
        description="Look up scan evidence.",
        input_model=Lookup,
        handler=lambda args: result,
    )


def registry(*tools):
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


def run(llm, tools=None, **kw):
    kw.setdefault("budget", Budget())
    return run_agent(
        role="mapper",
        task="map the app",
        llm=llm,
        tools=tools or registry(lookup_tool()),
        submit_name="submit_mapping",
        result_model=Mapping,
        **kw,
    )


def test_tool_call_then_submission_completes_in_one_run():
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("get_evidence", key="k1")]),
            LLMResponse(tool_calls=[call("submit_mapping", capability="s3", grade="DIRECT")]),
        ]
    )
    outcome = run(llm)
    assert outcome.status == "SUBMITTED"
    assert outcome.result == Mapping(capability="s3", grade="DIRECT")
    assert [s.tool for s in outcome.record.steps] == ["get_evidence", "submit_mapping"]
    assert outcome.record.turns == 2


def test_invalid_submission_returns_the_error_and_a_corrected_one_passes():
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("submit_mapping", capability="s3")]),
            LLMResponse(tool_calls=[call("submit_mapping", capability="s3", grade="DIRECT")]),
        ]
    )
    outcome = run(llm)
    assert outcome.status == "SUBMITTED"
    second_call_messages = llm.calls[1].messages
    assert any("grade" in m.content and m.role == "tool" for m in second_call_messages)
    assert outcome.record.steps[0].ok is False and outcome.record.steps[1].ok is True


def test_exceeding_the_turn_budget_fails_with_the_record():
    llm = FakeLLMClient([LLMResponse(tool_calls=[call("get_evidence", key="k")])] * 5)
    outcome = run(llm, budget=Budget(max_turns=3))
    assert outcome.status == "FAILED" and outcome.failure_reason == "BUDGET_TURNS"
    assert outcome.record.turns == 3 and len(outcome.record.steps) == 3
    assert outcome.result is None


def test_exceeding_the_token_budget_fails():
    big = LLMResponse(
        tool_calls=[call("get_evidence", key="k")], input_tokens=600, output_tokens=10
    )
    outcome = run(FakeLLMClient([big, big]), budget=Budget(max_input_tokens=1000))
    assert outcome.failure_reason == "BUDGET_INPUT_TOKENS"
    out = LLMResponse(tool_calls=[call("get_evidence", key="k")], output_tokens=80)
    outcome = run(FakeLLMClient([out, out]), budget=Budget(max_output_tokens=100))
    assert outcome.failure_reason == "BUDGET_OUTPUT_TOKENS"


def test_exceeding_the_wall_clock_budget_fails():
    ticks = iter([0.0, 1.0, 50.0, 99.0, 200.0])
    llm = FakeLLMClient([LLMResponse(tool_calls=[call("get_evidence", key="k")])] * 5)
    outcome = run(llm, budget=Budget(max_seconds=60), clock=lambda: next(ticks))
    assert outcome.failure_reason == "BUDGET_TIME"


def test_unknown_tool_is_rejected_and_never_executed():
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("run_shell", cmd="rm -rf /")]),
            LLMResponse(tool_calls=[call("submit_mapping", capability="x", grade="NONE")]),
        ]
    )
    outcome = run(llm)
    assert outcome.status == "SUBMITTED"
    assert outcome.record.steps[0].ok is False
    assert "not allowed" in outcome.record.steps[0].result_summary


@pytest.mark.parametrize(
    "name",
    ["bash", "shell_exec", "run_command", "http_get", "fetch_url", "aws_call", "get_credentials"],
)
def test_shell_network_and_credential_tools_cannot_be_registered(name):
    tool = ToolSpec(name=name, description="x", input_model=Lookup, handler=lambda a: "")
    with pytest.raises(ForbiddenToolError):
        ToolRegistry().register(tool)


def test_tool_arguments_are_validated_and_handler_errors_are_returned_to_the_model():
    def boom(args):
        raise RuntimeError("backend down")

    reg = registry(ToolSpec(name="get_evidence", description="d", input_model=Lookup, handler=boom))
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("get_evidence", wrong=1)]),
            LLMResponse(tool_calls=[call("get_evidence", key="k")]),
            LLMResponse(tool_calls=[call("submit_mapping", capability="x", grade="NONE")]),
        ]
    )
    outcome = run(llm, tools=reg)
    assert outcome.status == "SUBMITTED"
    assert [s.ok for s in outcome.record.steps] == [False, False, True]


def test_text_only_reply_counts_as_a_turn_and_is_nudged():
    llm = FakeLLMClient(
        [
            LLMResponse(text="I think it is s3"),
            LLMResponse(tool_calls=[call("submit_mapping", capability="s3", grade="DIRECT")]),
        ]
    )
    outcome = run(llm)
    assert outcome.status == "SUBMITTED" and outcome.record.turns == 2
    assert "submit_mapping" in llm.calls[1].messages[-1].content


def test_secrets_never_reach_the_record():
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("get_evidence", key=FAKE_AWS_KEY)]),
            LLMResponse(tool_calls=[call("submit_mapping", capability=FAKE_AWS_KEY, grade="NONE")]),
        ]
    )
    outcome = run(llm, tools=registry(lookup_tool(result=f"password = '{FAKE_AWS_KEY}'")))
    assert FAKE_AWS_KEY not in outcome.record.model_dump_json()


def test_prompt_version_is_recorded():
    llm = FakeLLMClient(
        [LLMResponse(tool_calls=[call("submit_mapping", capability="s", grade="NONE")])]
    )
    outcome = run(llm, prompt="You map apps.", prompt_version="v1")
    assert outcome.record.prompt_version == "v1" and outcome.record.role == "mapper"
    assert llm.calls[0].system == "You map apps."


def test_skill_loader_reads_only_the_requested_skill_and_examples_lazily(tmp_path):
    (tmp_path / "alpha/examples").mkdir(parents=True)
    (tmp_path / "alpha/SKILL.md").write_text("ALPHA GUIDE")
    (tmp_path / "alpha/examples/one.md").write_text("EXAMPLE ONE")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta/SKILL.md").write_text("BETA GUIDE")
    loader = SkillLoader(tmp_path, allowed=["alpha"])
    assert loader.load("alpha") == "ALPHA GUIDE"
    assert loader.load("alpha", part="examples") == "EXAMPLE ONE"
    assert loader.loaded == ["alpha"]
    with pytest.raises(SkillNotFoundError):
        loader.load("beta")
    with pytest.raises(SkillNotFoundError):
        SkillLoader(tmp_path, allowed=["gamma"]).load("gamma")


@pytest.mark.parametrize("bad", ["../alpha", "alpha/../beta", "a b", ""])
def test_skill_names_cannot_escape_the_skills_directory(tmp_path, bad):
    with pytest.raises(SkillNotFoundError):
        SkillLoader(tmp_path, allowed=[bad]).load(bad)


def test_load_skill_tool_feeds_the_run_and_is_recorded():
    loader = SkillLoader(REPO_SKILLS, allowed=["config-env"])
    tools = registry(make_load_skill_tool(loader))
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("load_skill", name="config-env")]),
            LLMResponse(tool_calls=[call("submit_mapping", capability="env", grade="DIRECT")]),
        ]
    )
    outcome = run(llm, tools=tools, skills=loader)
    assert outcome.status == "SUBMITTED"
    assert outcome.record.skills_loaded == ["config-env"]
    assert any("config-env" in m.content for m in llm.calls[1].messages if m.role == "tool")


@pytest.mark.parametrize("name", ["k8s-workload", "db-postgres", "network", "config-env"])
def test_first_cycle_skill_skeletons_exist(name):
    assert (REPO_SKILLS / name / "SKILL.md").read_text().strip()


def test_prompts_load_with_a_content_based_version():
    text, version = load_prompt("mapper")
    assert "mapper" in text and len(version) == 12
    assert load_prompt("mapper") == (text, version)
    assert load_prompt("tester")[1] != version
    with pytest.raises(FileNotFoundError):
        load_prompt("../secret")
