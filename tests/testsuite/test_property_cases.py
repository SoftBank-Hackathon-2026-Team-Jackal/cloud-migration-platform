"""Ticket 27-B: a write shape the Tester read from the source becomes property tests.

The model supplies only how the app takes a write and gives it back, with the source lines that
show it. The tests built from that shape expect the values they wrote themselves; the model never
writes an expected value.
"""

import pytest
from cmp_testsuite.basis import expectation_basis
from cmp_testsuite.evidence import check_evidence
from cmp_testsuite.model import ActionStep, Evidence, HttpStep
from cmp_testsuite.property import ShapeProposal, property_cases

GO_SHAPE = {
    "id": "messages",
    "write_path": "/api/messages",
    "write_body": {"client_msg_id": "std-{{run_id}}", "author": "cmp-test", "body": "value-{{run_id}}"},
    "write_status": 201,
    "id_from_response": "id",
    "value_field": "body",
    "read_back": "REPEAT_WRITE",
    "evidence": [Evidence(file="main.go", line=4)],
}  # fmt: skip


def shape(**changes):
    return ShapeProposal(**{**GO_SHAPE, **changes})


def test_a_shape_gives_a_round_trip_and_a_restart_test_that_cite_the_source():
    cases = property_cases(shape())
    assert [c.id for c in cases] == ["ai-messages-roundtrip", "ai-messages-restart"]
    for c in cases:
        assert c.source == "AI" and c.effect == "WRITES_DATA" and c.evidence == GO_SHAPE["evidence"]
        assert c.basis == "FREE" and expectation_basis(c) == "PROPERTY"
    restart = cases[1].steps
    assert [type(s).__name__ for s in restart] == ["HttpStep", "ActionStep", "HttpStep"]
    assert isinstance(restart[1], ActionStep) and restart[1].recover_seconds > 0


def test_the_built_tests_use_the_proposed_paths_and_statuses():
    roundtrip = property_cases(shape())[0]
    write, read = roundtrip.steps
    assert (write.method, write.path, write.expect.status) == ("POST", "/api/messages", 201)
    assert (read.method, read.path, read.expect.status) == ("POST", "/api/messages", 200)


def test_a_get_read_back_needs_a_read_path_with_the_id_in_it():
    ok = shape(read_back="GET", read_path="/api/messages/{{id}}")  # the server makes the ID
    read = property_cases(ok)[0].steps[1]
    assert isinstance(read, HttpStep) and read.method == "GET"
    assert read.path == "/api/messages/{{item_id}}"
    chosen = shape(
        read_back="GET", read_path="/api/messages/{{id}}", id_from_response=None,
        write_body={"id": "std-{{run_id}}", "body": "value-{{run_id}}"},
    )  # fmt: skip
    assert property_cases(chosen)[0].steps[1].path == "/api/messages/std-{{run_id}}-rt"
    with pytest.raises(ValueError):  # no server ID and no ID the test chose: nothing to read by
        shape(read_back="GET", read_path="/api/messages/{{id}}", id_from_response=None)
    with pytest.raises(ValueError):
        shape(read_back="GET", read_path=None)
    with pytest.raises(ValueError):
        shape(read_back="GET", read_path="/api/messages")


@pytest.mark.parametrize(
    "changes",
    [
        {"write_path": "/api/{{x}}"},
        {"write_path": "api/messages"},
        {"write_body": {"body": "value-{{run_id}}"}, "value_field": "text"},
        {"write_body": {"client_msg_id": "std-{{run_id}}", "body": "hello"}},
        {"write_status": 99},
        {"evidence": []},
        {"id": "Has Space"},
    ],
)
def test_a_shape_that_cannot_make_a_sound_test_is_refused(changes):
    with pytest.raises(ValueError):
        shape(**changes)


def test_the_built_tests_pass_the_evidence_check_against_the_real_source(tmp_path):
    (tmp_path / "main.go").write_text(
        'package main\n\nfunc r(m *Mux) {\n\tm.HandleFunc("POST /api/messages", c)\n}\n'
    )
    assert all(check_evidence(tmp_path, c) is None for c in property_cases(shape()))
    (tmp_path / "main.go").write_text("package main\n")
    assert property_cases(shape())[0].evidence and check_evidence(tmp_path, property_cases(shape())[0]) is not None  # fmt: skip
