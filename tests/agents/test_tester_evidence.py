"""Ticket 27-B: the Tester reads the source and proposes shapes and probes, not expected values."""

import pytest
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_agents.tester import SuiteRequest, generate_tests
from cmp_scanners.routes import HttpRoute

KEY = "AKIAABCDEFGHIJKLMNOP"
GO = (
    "package main\n\nfunc r(m *Mux) {\n"
    '\tm.HandleFunc("POST /api/messages", create)\n'
    '\tm.HandleFunc("GET /api/messages", list)\n}\n'
    f'var awsKey = "{KEY}"\n'
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
PROBE = {"id": "messages", "path": "/api/messages", "evidence": [{"file": "main.go", "line": 5}]}
FREE_CASE = {
    "id": "ai-total",
    "source": "AI",
    "effect": "READ_ONLY",
    "steps": [{"path": "/total", "expect": {"status": 200, "json_equals": {"total": 10}}}],
}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "main.go").write_text(GO)
    (tmp_path / ".env").write_text("SECRET=hunter2\n")
    return tmp_path


def request(source, **changes):
    fields = {
        "usage_summary": "postgres via pgx",
        "http_routes": [
            HttpRoute(method="POST", path="/api/messages", file="main.go", line=4),
            HttpRoute(method=None, path="/healthz", file="main.go", line=9),
        ],
        "workspace": source,
        "origin_available": False,
    }
    return SuiteRequest(**{**fields, **changes})


def submit(**fields):
    return LLMResponse(tool_calls=[call("submit_tests", **fields)])


def test_shapes_become_property_tests_and_probes_are_handed_back(workspace):
    llm = FakeLLMClient([submit(shapes=[SHAPE], probes=[PROBE])])
    result = generate_tests(request(workspace), llm=llm)
    assert result.status == "GENERATED"
    assert [c.id for c in result.cases] == ["ai-messages-roundtrip", "ai-messages-restart"]
    assert [p.id for p in result.probes] == ["messages"]


def test_the_routes_the_scanner_found_are_part_of_the_task_with_their_place(workspace):
    llm = FakeLLMClient([submit(shapes=[SHAPE])])
    generate_tests(request(workspace), llm=llm)
    sent = " ".join(m.content for m in llm.calls[0].messages)
    assert "POST /api/messages (main.go:4)" in sent and "ANY /healthz (main.go:9)" in sent


def test_a_model_cannot_claim_its_own_expected_values_came_from_a_recording(workspace):
    recorded = {**FREE_CASE, "basis": "RECORDED"}
    llm = FakeLLMClient([submit(cases=[recorded]), submit(shapes=[SHAPE])])
    result = generate_tests(request(workspace, origin_available=True), llm=llm)
    assert result.status == "GENERATED" and len(llm.calls) == 2


def test_without_an_origin_test_resource_free_cases_are_not_accepted(workspace):
    llm = FakeLLMClient([submit(cases=[FREE_CASE]), submit(shapes=[SHAPE])])
    result = generate_tests(request(workspace, origin_available=False), llm=llm)
    assert [c.id for c in result.cases] == ["ai-messages-roundtrip", "ai-messages-restart"]
    assert len(llm.calls) == 2  # the first submission was handed back for correction


def test_with_an_origin_test_resource_free_cases_are_still_allowed(workspace):
    llm = FakeLLMClient([submit(cases=[FREE_CASE])])
    result = generate_tests(request(workspace, origin_available=True), llm=llm)
    assert [c.id for c in result.cases] == ["ai-total"]


def test_a_submission_with_nothing_in_it_is_handed_back(workspace):
    llm = FakeLLMClient([submit(shapes=[], probes=[], cases=[]), submit(probes=[PROBE])])
    result = generate_tests(request(workspace), llm=llm)
    assert result.status == "GENERATED" and result.cases == [] and len(result.probes) == 1


def test_a_shape_whose_value_is_not_the_written_one_is_handed_back(workspace):
    bad = {**SHAPE, "write_body": {"client_msg_id": "std-{{run_id}}", "body": "hello"}}
    llm = FakeLLMClient([submit(shapes=[bad]), submit(shapes=[SHAPE])])
    assert generate_tests(request(workspace), llm=llm).status == "GENERATED"
    assert len(llm.calls) == 2


# ------------------------------------------------------------------------ the snippet tool


def snippet_reply(workspace, **arguments):
    llm = FakeLLMClient(
        [LLMResponse(tool_calls=[call("get_snippet", **arguments)]), submit(shapes=[SHAPE])]
    )
    generate_tests(request(workspace), llm=llm)
    return llm.calls[1].messages[-1].content


def test_the_snippet_tool_shows_numbered_lines_with_secrets_masked(workspace):
    text = snippet_reply(workspace, file="main.go", line=4, count=4)
    assert '4: \tm.HandleFunc("POST /api/messages", create)' in text
    assert "6" in text and KEY not in text


def test_the_snippet_tool_refuses_env_files_and_paths_outside_the_source(workspace, tmp_path):
    for name in (".env", "../outside.go", "/etc/hosts"):
        text = snippet_reply(workspace, file=name, line=1)
        assert text.startswith("error:") and "hunter2" not in text


def test_the_snippet_tool_never_returns_more_than_a_page(workspace):
    big = workspace / "big.go"
    big.write_text("\n".join(f"line {n}" for n in range(1, 500)))
    text = snippet_reply(workspace, file="big.go", line=1, count=400)
    assert len(text.splitlines()) <= 60


def test_there_is_no_snippet_tool_without_a_source(workspace):
    llm = FakeLLMClient([submit(shapes=[SHAPE])])
    generate_tests(request(workspace, workspace=None), llm=llm)
    assert "get_snippet" not in {t.name for t in llm.calls[0].tools}


# ------------------------------------------------------------------------------- the skill


def test_the_test_authoring_skill_can_be_loaded_when_a_skills_folder_is_given(workspace, tmp_path):
    skills = tmp_path / "skills"
    (skills / "test-authoring").mkdir(parents=True)
    (skills / "test-authoring" / "SKILL.md").write_text("# test-authoring\nWrite what you read.")
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("load_skill", name="test-authoring")]),
            submit(shapes=[SHAPE]),
        ]
    )
    result = generate_tests(request(workspace, skills_root=skills), llm=llm)
    assert "Write what you read." in llm.calls[1].messages[-1].content
    assert result.record.skills_loaded == ["test-authoring"]
    plain = FakeLLMClient([submit(shapes=[SHAPE])])
    generate_tests(request(workspace), llm=plain)
    assert "load_skill" not in {t.name for t in plain.calls[0].tools}
