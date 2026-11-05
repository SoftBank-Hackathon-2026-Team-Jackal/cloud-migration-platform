import json

import pytest
from cmp_agents.fixer import (
    CodeFix,
    FixerError,
    FixInput,
    RequirementsFix,
    apply_code_fix,
    apply_requirements_fix,
    propose_fix,
)
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_contracts.provisioning_v2 import RequirementsV2

KEY = "AKIAABCDEFGHIJKLMNOP"


def requirements():
    return RequirementsV2.model_validate(
        {
            "contract_version": "2",
            "resources": [
                {"id": "api", "kind": "APPLICATION", "replicas": 1, "cpu_millicores": 100,
                 "memory_mib": 128, "port": 8080, "health_path": "/healthz", "env_bindings": {}},
                {"id": "db", "kind": "POSTGRESQL", "version": "16", "extensions": [],
                 "database": {"name": "app", "username": "app"}, "cpu_millicores": 250,
                 "memory_mib": 512, "storage_gib": 1},
            ],
            "network_intent": [
                {"source_resource_id": "api", "destination_resource_id": "db", "port": 5432}
            ],
        }
    )  # fmt: skip


def submit(name, **kw):
    return LLMResponse(tool_calls=[call(name, **kw)])


def data(failure_class, **kw):
    return FixInput(
        failure_class=failure_class,
        reasons=["EGRESS_BLOCKED:api.pay.example.com:443"],
        requirements_json=json.dumps(requirements().model_dump(mode="json")) + f" key {KEY}",
        logs=f"dial tcp api.pay.example.com:443 refused {KEY}",
        tests={"std-health": {"steps": ["GET /healthz"]}},
        **kw,
    )


def test_a_spec_fix_adds_the_missing_external_intent():
    llm = FakeLLMClient([submit("submit_requirements_fix", reason="payment API is called",
        add_intents=[{"source_resource_id": "api", "external_host": "api.pay.example.com", "port": 443}])])  # fmt: skip
    outcome = propose_fix(data("SPEC"), llm=llm)
    assert outcome.status == "PROPOSED" and outcome.code_fix is None
    fixed = apply_requirements_fix(requirements(), outcome.requirements_fix)
    assert [(i.external_host, i.port) for i in fixed.network_intent if i.external_host] == [
        ("api.pay.example.com", 443)
    ]
    sent = llm.calls[0].system + " ".join(m.content for m in llm.calls[0].messages)
    assert KEY not in sent and "std-health" in sent


def test_a_port_edit_is_applied_and_everything_else_is_kept():
    fix = RequirementsFix(
        reason="wrong port", edits=[{"resource_id": "api", "field": "port", "value": 9898}]
    )
    fixed = apply_requirements_fix(requirements(), fix)
    api = next(r for r in fixed.resources if r.id == "api")
    assert api.port == 9898 and fixed.network_intent == requirements().network_intent


def test_edits_that_cannot_apply_are_refused():
    with pytest.raises(FixerError) as unknown:
        apply_requirements_fix(
            requirements(),
            RequirementsFix(
                reason="x", edits=[{"resource_id": "nope", "field": "port", "value": 1}]
            ),
        )
    assert unknown.value.code == "UNKNOWN_RESOURCE"
    with pytest.raises(FixerError) as wrong:
        apply_requirements_fix(
            requirements(),
            RequirementsFix(reason="x", edits=[{"resource_id": "db", "field": "port", "value": 1}]),
        )
    assert wrong.value.code == "FIELD_NOT_ON_RESOURCE"
    with pytest.raises(FixerError) as invalid:
        apply_requirements_fix(
            requirements(),
            RequirementsFix(
                reason="x", edits=[{"resource_id": "api", "field": "port", "value": 70000}]
            ),
        )
    assert invalid.value.code == "FIX_MAKES_REQUIREMENTS_INVALID"
    with pytest.raises(FixerError) as missing:
        apply_requirements_fix(
            requirements(),
            RequirementsFix(
                reason="x",
                remove_intents=[
                    {"source_resource_id": "api", "external_host": "x.example.com", "port": 1}
                ],
            ),
        )
    assert missing.value.code == "INTENT_NOT_FOUND"


def test_an_empty_requirements_fix_is_not_a_fix():
    with pytest.raises(ValueError):
        RequirementsFix(reason="nothing")


@pytest.mark.parametrize(
    "path",
    ["main_test.go", "tests/test_api.py", "internal/orders/orders_test.go", "web/app.test.ts",
     "pkg/testdata/seed.json", "conftest.py", ".env", "config/.env.production", "../escape.go",
     "/etc/passwd", ".git/config"],
)  # fmt: skip
def test_a_fix_may_not_touch_tests_environment_files_or_paths_outside_the_workspace(path):
    with pytest.raises(ValueError):
        CodeFix(reason="x", files=[{"path": path, "content": "package x"}])


def test_a_code_fix_is_written_into_the_workspace(tmp_path):
    fix = CodeFix(
        reason="read the port from the environment",
        files=[{"path": "cmd/main.go", "content": "package main\n"}],
    )
    assert apply_code_fix(tmp_path, fix) == ["cmd/main.go"]
    assert (tmp_path / "cmd/main.go").read_text() == "package main\n"


def test_a_code_fix_that_tries_a_test_file_is_rejected_and_the_agent_can_correct_it():
    llm = FakeLLMClient([
        submit("submit_code_fix", reason="make test pass", files=[{"path": "main_test.go", "content": "x"}]),
        submit("submit_code_fix", reason="fix the handler", files=[{"path": "main.go", "content": "package main\n"}]),
    ])  # fmt: skip
    outcome = propose_fix(data("CODE"), llm=llm, workspace="/nonexistent-but-unused")
    assert outcome.status == "PROPOSED" and [f.path for f in outcome.code_fix.files] == ["main.go"]
    steps = outcome.record.steps
    assert steps[0].ok is False and "TEST_FILE_CHANGE_REFUSED" in steps[0].result_summary


def test_the_fixer_reads_workspace_files_masked_and_never_env_files(tmp_path):
    (tmp_path / "main.go").write_text(f'var k = "{KEY}"\n')
    (tmp_path / ".env").write_text("DB_PASSWORD=hunter2\n")
    llm = FakeLLMClient([
        LLMResponse(tool_calls=[call("read_file", path="main.go"), call("read_file", path=".env"), call("read_file", path="../x")]),
        submit("submit_code_fix", reason="r", files=[{"path": "main.go", "content": "package main\n"}]),
    ])  # fmt: skip
    outcome = propose_fix(data("CODE"), llm=llm, workspace=tmp_path)
    seen = " ".join(m.content for m in llm.calls[1].messages if m.role == "tool")
    assert KEY not in seen and "hunter2" not in seen and "outside the workspace" in seen
    assert outcome.status == "PROPOSED"


@pytest.mark.parametrize("klass", ["INFRA", "RENDER"])
def test_infra_and_render_failures_never_call_the_model(klass):
    llm = FakeLLMClient([])
    outcome = propose_fix(data(klass), llm=llm, workspace="/x")
    assert outcome.status == "NOT_CALLED" and llm.calls == []


def test_a_code_fix_needs_a_workspace():
    assert propose_fix(data("BUILD"), llm=FakeLLMClient([])).reason == "WORKSPACE_REQUIRED"


def test_running_out_of_turns_fails_with_a_reason():
    from cmp_agents.runtime import Budget

    llm = FakeLLMClient([LLMResponse(text="thinking")] * 6)
    outcome = propose_fix(data("SPEC"), llm=llm, budget=Budget(max_turns=2))
    assert outcome.status == "FAILED" and outcome.reason == "BUDGET_TURNS"
