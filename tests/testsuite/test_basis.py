"""Ticket 27-B: which tests know their own answer.

A test whose expected values are the ones it wrote itself (write X, read X) needs no outside
answer key. A test that expects some other literal (a computed total, a status text) does: that
value came from somebody's idea of what the app does, and an AI must not supply it unchecked.
"""

import pytest
from cmp_testsuite import HttpStep, SuiteCase
from cmp_testsuite.basis import expectation_basis
from cmp_testsuite.model import Expect
from cmp_testsuite.standard import AppApi, standard_cases


def case(*steps):
    return SuiteCase(id="ai-x", source="AI", effect="WRITES_DATA", steps=list(steps))


def write(body, **expect):
    return HttpStep(method="POST", path="/items", json_body=body, expect=Expect(**expect))


def read(**expect):
    return HttpStep(path="/items/1", expect=Expect(**expect))


def test_reading_back_what_the_test_wrote_is_self_evident():
    c = case(
        write({"name": "n-{{run_id}}"}, status=201),
        read(status=200, json_equals={"name": "n-{{run_id}}"}),
    )
    assert expectation_basis(c) == "PROPERTY"


def test_a_literal_the_test_never_sent_needs_an_outside_answer():
    c = case(
        write({"qty": 3}, status=201),
        read(status=200, json_equals={"total": 2700}),
    )
    assert expectation_basis(c) == "FREE"


def test_a_value_sent_only_after_the_check_does_not_count():
    c = case(
        read(status=200, json_equals={"name": "n-{{run_id}}"}),
        write({"name": "n-{{run_id}}"}, status=201),
    )
    assert expectation_basis(c) == "FREE"


def test_an_echo_in_the_same_step_that_sent_the_value_counts():
    c = case(write({"name": "n-{{run_id}}"}, status=201, json_equals={"name": "n-{{run_id}}"}))
    assert expectation_basis(c) == "PROPERTY"


def test_comparing_with_a_value_saved_from_an_earlier_answer_is_self_evident():
    first = HttpStep(
        method="POST", path="/items", json_body={"a": 1}, save={"item_id": "id"},
        expect=Expect(status=201),
    )  # fmt: skip
    c = case(first, read(status=200, json_equals_var={"id": "item_id"}))
    assert expectation_basis(c) == "PROPERTY"


def test_one_outside_literal_is_enough_to_need_an_answer():
    c = case(
        write({"name": "n-{{run_id}}"}, status=201),
        read(status=200, json_equals={"name": "n-{{run_id}}", "state": "ACTIVE"}),
    )
    assert expectation_basis(c) == "FREE"


def test_body_text_must_also_come_from_what_was_sent():
    sent = case(write({"name": "n-{{run_id}}"}, status=201), read(body_contains="n-{{run_id}}"))
    other = case(write({"name": "n-{{run_id}}"}, status=201), read(body_contains="ACTIVE"))
    assert expectation_basis(sent) == "PROPERTY"
    assert expectation_basis(other) == "FREE"


def test_a_status_only_test_expects_no_value_and_is_not_a_property_test():
    assert expectation_basis(case(read(status=200))) == "FREE"


@pytest.mark.parametrize(
    "api",
    [
        AppApi(health_path="/h", write_path="/w", read_path="/r/{{id}}"),
        AppApi(
            health_path="/healthz",
            write_path="/api/messages",
            write_body={"client_msg_id": "std-{{run_id}}", "body": "value-{{run_id}}"},
            write_status=201,
            id_from_response="id",
            value_field="body",
            read_back="REPEAT_WRITE",
        ),
    ],
)
def test_the_standard_round_trip_and_restart_tests_are_property_tests(api):
    by_id = {c.id: c for c in standard_cases(api)}
    assert expectation_basis(by_id["std-db-roundtrip"]) == "PROPERTY"
    assert expectation_basis(by_id["std-survives-db-restart"]) == "PROPERTY"
