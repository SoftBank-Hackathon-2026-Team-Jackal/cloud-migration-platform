from pathlib import Path

import pytest
from cmp_agents.catalog import load_catalog
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_agents.mapper import MapperInput, map_usage
from cmp_contracts.analysis import Evidence, MappingRow

ROOT = Path(__file__).parent.parent.parent
FAKE_KEY = "AKIAABCDEFGHIJKLMNOP"


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(ROOT / "catalog", skills_root=ROOT / "skills")


def proposal(capability="postgres", usage="pgx driver", **kw):
    fields = {
        "id": "tmp-1",
        "capability": capability,
        "usage": usage,
        "grade": "DIRECT",
        "required_proposed": True,
        "required_reason": "store of record",
        "disposition": "MIGRATE",
        "loses": [],
        "evidence": [{"kind": "CODE", "locator": "main.go:13"}],
    }
    fields.update(kw)
    return fields


def submit(*proposals):
    return LLMResponse(tool_calls=[call("submit_mapping", rows=list(proposals))])


def run(llm, catalog, summary="ENV_READ DATABASE_URL main.go:13", previous=(), **kw):
    return map_usage(
        MapperInput(
            evidence_summary=summary,
            evidence_items=kw.pop("items", {}),
            previous_rows=list(previous),
        ),
        llm=llm,
        catalog=catalog,
        skills_root=ROOT / "skills",
        **kw,
    )


def test_proposals_become_unconfirmed_rows_without_treatment_or_verification(catalog):
    result = run(FakeLLMClient([submit(proposal())]), catalog)
    assert result.status == "MAPPED"
    row = result.rows[0]
    assert (row.required, row.treatment, row.status) == (None, None, "UNVERIFIED")
    assert row.required_proposed is True and row.required_reason
    assert row.evidence[0].verified is False


def test_previous_ids_are_kept_and_object_storage_is_kept_at_the_source(catalog):
    previous = [
        MappingRow(
            id="r7",
            capability="postgres",
            usage="pgx driver",
            grade="DIRECT",
            required_proposed=True,
            required_reason="x",
            required=None,
            disposition="MIGRATE",
            loses=[],
            treatment=None,
            status="EVIDENCE_CHECKED",
            evidence=[Evidence(kind="CODE", locator="main.go:13", verified=True)],
        )
    ]
    llm = FakeLLMClient(
        [submit(proposal(id="new-1"), proposal("object-storage", "uploads", id="new-2"))]
    )
    rows = run(llm, catalog, previous=previous).rows
    assert rows[0].id == "r7"
    assert rows[1].disposition == "KEEP"


def test_the_llm_only_sees_masked_input_and_the_catalog_candidates(catalog):
    summary = (
        f"ENV_READ DATABASE_URL main.go:13\npassword = '{FAKE_KEY}'\neks.amazonaws.com/role-arn"
    )
    llm = FakeLLMClient([submit(proposal())])
    run(llm, catalog, summary=summary, items={"e1": f"token {FAKE_KEY}"})
    seen = llm.calls[0].messages[0].content + llm.calls[0].system
    assert FAKE_KEY not in seen
    assert "pod-identity" in seen  # catalog candidate from the evidence text


def test_evidence_tool_returns_masked_items(catalog):
    llm = FakeLLMClient(
        [LLMResponse(tool_calls=[call("get_evidence", key="e1")]), submit(proposal())]
    )
    result = run(llm, catalog, items={"e1": f"password = '{FAKE_KEY}'"})
    assert result.status == "MAPPED"
    assert FAKE_KEY not in " ".join(m.content for m in llm.calls[1].messages)


def test_skills_for_candidate_capabilities_can_be_loaded_but_only_those(catalog):
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("load_skill", name="db-postgres")]),
            LLMResponse(tool_calls=[call("load_skill", name="config-env")]),
            submit(proposal()),
        ]
    )
    result = run(llm, catalog, summary="CREATE EXTENSION pgcrypto")
    assert result.record.skills_loaded == ["db-postgres"]
    tool_messages = [m.content for m in llm.calls[2].messages if m.role == "tool"]
    assert any("not available" in t for t in tool_messages)


def test_invalid_output_is_retried_then_handed_to_human_review(catalog):
    bad = LLMResponse(tool_calls=[call("submit_mapping", rows=[{"capability": "postgres"}])])
    llm = FakeLLMClient([bad] * 12)
    result = run(llm, catalog, max_attempts=3, max_turns=2)
    assert result.status == "NEEDS_REVIEW" and result.rows == []
    assert result.attempts == 3 and result.failure_reason == "BUDGET_TURNS"


def test_a_retry_that_succeeds_returns_the_mapping(catalog):
    bad = LLMResponse(tool_calls=[call("submit_mapping", rows=[{}])])
    llm = FakeLLMClient([bad, bad, submit(proposal())])
    result = run(llm, catalog, max_attempts=2, max_turns=2)
    assert result.status == "MAPPED" and result.attempts == 2


def test_catalog_outside_capabilities_are_kept_as_proposals(catalog):
    result = run(FakeLLMClient([submit(proposal("quantum-db", "qubits"))]), catalog)
    assert result.rows[0].capability == "quantum-db" and result.rows[0].status == "UNVERIFIED"


@pytest.mark.parametrize("field", ["evidence", "required_reason"])
def test_every_row_needs_a_required_reason_and_evidence(catalog, field):
    empty = proposal(**{field: [] if field == "evidence" else ""})
    result = run(FakeLLMClient([submit(empty)] * 6), catalog, max_attempts=2, max_turns=2)
    assert result.status == "NEEDS_REVIEW"
