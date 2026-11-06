"""Ticket 27-B: an AI test must point at the source lines it was written from.

`check_evidence` looks at the real files of the fetched source: the cited file and line must exist
and the route each HTTP step calls must appear at the cited place. Nothing here asks a model.
"""

import pytest
from cmp_testsuite import HttpStep, SuiteCase, dump_bundle
from cmp_testsuite.evidence import Evidence, check_evidence
from cmp_testsuite.model import Expect, Suite

GO_SOURCE = """package main

import "net/http"

func routes(mux *http.ServeMux) {
	mux.HandleFunc("GET /healthz", health)
	mux.HandleFunc("POST /api/messages", createMessage)
	mux.HandleFunc("GET /api/messages", listMessages)
}

func health(w http.ResponseWriter, r *http.Request) {}
"""


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "server.go").write_text(GO_SOURCE)
    return tmp_path


def case(*paths, evidence=()):
    return SuiteCase(
        id="ai-sample",
        source="AI",
        effect="READ_ONLY",
        evidence=[Evidence(file=f, line=n) for f, n in evidence],
        steps=[HttpStep(path=p, expect=Expect(status=200, body_contains="x")) for p in paths],
    )


def test_a_cited_line_that_holds_the_route_passes(workspace):
    c = case("/api/messages", evidence=[("cmd/server.go", 8)])
    assert check_evidence(workspace, c) is None


def test_the_route_may_sit_a_couple_of_lines_from_the_cited_line(workspace):
    c = case("/api/messages", evidence=[("cmd/server.go", 9)])
    assert check_evidence(workspace, c) is None


def test_a_test_without_evidence_is_refused(workspace):
    assert check_evidence(workspace, case("/api/messages")) == "NO_EVIDENCE"


def test_a_missing_file_is_refused(workspace):
    c = case("/api/messages", evidence=[("cmd/nope.go", 8)])
    assert check_evidence(workspace, c) == "EVIDENCE_FILE_MISSING"


@pytest.mark.parametrize("name", ["../outside.go", "/etc/hosts", "cmd/../../x.go"])
def test_a_file_outside_the_source_is_refused_like_a_missing_one(workspace, tmp_path, name):
    (tmp_path.parent / "outside.go").write_text("/api/messages")
    c = case("/api/messages", evidence=[(name, 1)])
    assert check_evidence(workspace, c) == "EVIDENCE_FILE_MISSING"


def test_a_line_past_the_end_of_the_file_is_refused(workspace):
    c = case("/api/messages", evidence=[("cmd/server.go", 400)])
    assert check_evidence(workspace, c) == "EVIDENCE_LINE_MISSING"


def test_a_route_that_is_not_near_the_cited_line_is_refused(workspace):
    c = case("/api/messages", evidence=[("cmd/server.go", 3)])
    assert check_evidence(workspace, c) == "EVIDENCE_MISMATCH /api/messages"


def test_every_route_the_test_calls_needs_its_own_support(workspace):
    c = case("/api/messages", "/api/orders", evidence=[("cmd/server.go", 8)])
    assert check_evidence(workspace, c) == "EVIDENCE_MISMATCH /api/orders"


def test_several_citations_can_cover_several_routes(workspace):
    c = case(
        "/api/messages",
        "/healthz",
        evidence=[("cmd/server.go", 8), ("cmd/server.go", 7)],
    )
    assert check_evidence(workspace, c) is None


def test_the_fixed_part_of_a_path_with_a_variable_is_what_must_appear(workspace):
    c = case("/api/messages/{{id}}", evidence=[("cmd/server.go", 8)])
    assert check_evidence(workspace, c) is None


def test_a_path_that_is_only_a_variable_needs_no_route_support(workspace):
    c = case("{{health_path}}", evidence=[("cmd/server.go", 1)])
    assert check_evidence(workspace, c) is None


def test_a_case_without_evidence_keeps_its_old_bytes():
    """Standard suites are pinned by hash; the new field must not change their bytes."""
    plain = SuiteCase(
        id="std-health",
        source="STANDARD",
        effect="READ_ONLY",
        steps=[HttpStep(path="/healthz", expect=Expect(status=200))],
    )
    assert "evidence" not in plain.model_dump(mode="json")  # `dump_bundle` writes exactly this
    assert dump_bundle(Suite(name="s", cases=[plain])) == dump_bundle(
        Suite(name="s", cases=[SuiteCase.model_validate(plain.model_dump(mode="json"))])
    )
    assert "evidence" in case("/api/messages", evidence=[("a.go", 1)]).model_dump(mode="json")
