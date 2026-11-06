from contextlib import contextmanager

from cmp_testsuite import CaseResult, Expect, HttpStep, SuiteCase
from cmp_testsuite.standard import AppApi, standard_cases
from cmp_testsuite.validate import pin_suite, validate_tests


def ai_case(case_id="ai-1", *, expect=None):
    return SuiteCase(
        id=case_id, source="AI", effect="READ_ONLY",
        steps=[HttpStep(path="/items/1", expect=expect or Expect(status=200, json_equals={"value": "x"}))],
    )  # fmt: skip


class World:
    """target passes only while the dependency is up; origin always passes unless told not to."""

    def __init__(self, origin_ok=True, depends=True):
        self.up, self.origin_ok, self.depends = True, origin_ok, depends

    def target(self, case):
        ok = self.up or not self.depends
        return CaseResult(id=case.id, status="PASSED" if ok else "FAILED")

    def origin(self, case):
        return CaseResult(id=case.id, status="PASSED" if self.origin_ok else "FAILED")

    @contextmanager
    def cut(self):
        self.up = False
        try:
            yield
        finally:
            self.up = True


def check(world, *cases, origin=True, cut=True):
    return validate_tests(
        list(cases),
        target=world.target,
        origin=world.origin if origin else None,
        cut_dependency=world.cut if cut else None,
    )


def test_a_test_meeting_all_conditions_is_accepted():
    result = check(World(), ai_case())
    assert [(v.case_id, v.accepted, v.reason) for v in result.verdicts] == [("ai-1", True, None)]
    assert [c.id for c in result.accepted] == ["ai-1"]


def test_a_status_code_only_test_is_refused_by_condition_two():
    result = check(World(), ai_case(expect=Expect(status=200)))
    assert result.verdicts[0].reason == "NO_VALUE_ASSERTION" and result.accepted == []


def test_a_test_that_still_passes_without_the_dependency_is_refused_by_condition_three():
    result = check(World(depends=False), ai_case())
    assert result.verdicts[0].reason == "PASSES_WITHOUT_DEPENDENCY"


def test_dependency_is_restored_after_the_check():
    world = World()
    check(world, ai_case())
    assert world.up


def test_a_test_failing_on_the_origin_is_refused_by_condition_one():
    assert check(World(origin_ok=False), ai_case()).verdicts[0].reason == "ORIGIN_FAILED"


def test_without_an_origin_or_a_way_to_cut_nothing_is_accepted():
    assert check(World(), ai_case(), origin=False).verdicts[0].reason == "ORIGIN_UNAVAILABLE"
    assert check(World(), ai_case(), cut=False).verdicts[0].reason == "CUT_UNAVAILABLE"


def test_standard_cases_cover_health_roundtrip_and_restart():
    cases = standard_cases(
        AppApi(health_path="/healthz", write_path="/items", read_path="/items/{{id}}")
    )
    assert [c.id for c in cases] == ["std-health", "std-db-roundtrip", "std-survives-db-restart"]
    assert standard_cases(AppApi(health_path="/healthz"))[0].id == "std-health"
    assert len(standard_cases(AppApi(health_path="/healthz"))) == 1


def test_pinning_gives_the_same_bytes_for_the_same_accepted_set():
    standard = standard_cases(AppApi(health_path="/healthz"))
    one = pin_suite("demo", standard, [ai_case()])[1]
    assert one == pin_suite("demo", standard, [ai_case()])[1]
    assert one != pin_suite("demo", standard, [])[1]


# ---------------------------------------------------------------------------------- ticket 27-B
# Condition 1 no longer needs an origin test resource for a test that knows its own answer: it
# needs the test to cite real source lines. A test with some other expected literal still needs an
# outside answer (an origin run), because an AI's idea of that value is not evidence.

import pytest
from cmp_testsuite.model import Evidence

SOURCE = 'package main\n\nfunc r(m *Mux) {\n\tm.HandleFunc("POST /items", create)\n}\n'


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "main.go").write_text(SOURCE)
    return tmp_path


def property_case(case_id="ai-prop", *, evidence=((("main.go"), 4),)):
    return SuiteCase(
        id=case_id, source="AI", effect="WRITES_DATA",
        evidence=[Evidence(file=f, line=n) for f, n in evidence],
        steps=[
            HttpStep(
                method="POST", path="/items", json_body={"name": "n-{{run_id}}"},
                expect=Expect(status=201, json_equals={"name": "n-{{run_id}}"}),
            )
        ],
    )  # fmt: skip


def free_case(case_id="ai-free"):
    return SuiteCase(
        id=case_id, source="AI", effect="READ_ONLY",
        evidence=[Evidence(file="main.go", line=4)],
        steps=[HttpStep(path="/items", expect=Expect(status=200, json_equals={"total": 2700}))],
    )  # fmt: skip


def check_with(world, workspace, *cases, origin=False, cut=True):
    return validate_tests(
        list(cases),
        target=world.target,
        origin=world.origin if origin else None,
        cut_dependency=world.cut if cut else None,
        workspace=workspace,
    )


def test_a_property_test_with_real_evidence_needs_no_origin(workspace):
    result = check_with(World(), workspace, property_case())
    assert [(v.accepted, v.reason) for v in result.verdicts] == [(True, None)]


def test_a_property_test_without_evidence_is_refused(workspace):
    result = check_with(World(), workspace, property_case(evidence=()))
    assert result.verdicts[0].reason == "NO_EVIDENCE" and result.accepted == []


def test_evidence_that_does_not_hold_is_refused(workspace):
    result = check_with(World(), workspace, property_case(evidence=[("main.go", 1)]))
    assert result.verdicts[0].reason == "EVIDENCE_MISMATCH /items"


def test_an_expectation_the_test_did_not_write_still_needs_an_origin_run(workspace):
    result = check_with(World(), workspace, free_case())
    assert result.verdicts[0].reason == "ORIGIN_UNAVAILABLE"


def test_an_origin_run_can_still_vouch_for_an_outside_expectation(workspace):
    result = check_with(World(), workspace, free_case(), origin=True)
    assert [(v.accepted, v.reason) for v in result.verdicts] == [(True, None)]


def test_evidence_is_checked_even_when_an_origin_run_would_pass(workspace):
    bad = free_case()
    bad.evidence = [Evidence(file="main.go", line=1)]
    result = check_with(World(), workspace, bad, origin=True)
    assert result.verdicts[0].reason == "EVIDENCE_MISMATCH /items"


def test_without_a_source_a_property_test_cannot_be_vouched_for():
    result = check_with(World(), None, property_case())
    assert result.verdicts[0].reason == "EVIDENCE_UNAVAILABLE"


def test_the_other_conditions_still_apply_to_a_property_test(workspace):
    failing = World()
    failing.up = False
    assert check_with(failing, workspace, property_case()).verdicts[0].reason == "TARGET_FAILED"
    independent = World(depends=False)
    result = check_with(independent, workspace, property_case())
    assert result.verdicts[0].reason == "PASSES_WITHOUT_DEPENDENCY"


def recorded_case(case_id="ai-0rec-items", *, line=4):
    return SuiteCase(
        id=case_id, source="AI", effect="READ_ONLY", basis="RECORDED",
        evidence=[Evidence(file="main.go", line=line)],
        steps=[HttpStep(path="/items", expect=Expect(status=200, json_equals={"total": 2700}))],
    )  # fmt: skip


def test_a_recorded_test_needs_no_origin_because_its_answer_was_taken_from_the_app(workspace):
    result = check_with(World(), workspace, recorded_case())
    assert [(v.accepted, v.reason) for v in result.verdicts] == [(True, None)]


def test_a_recorded_test_still_has_to_cite_the_source(workspace):
    result = check_with(World(), workspace, recorded_case(line=1))
    assert result.verdicts[0].reason == "EVIDENCE_MISMATCH /items"


def test_a_recorded_test_that_does_not_depend_on_a_migrated_service_is_dropped(workspace):
    result = check_with(World(depends=False), workspace, recorded_case())
    assert result.verdicts[0].reason == "PASSES_WITHOUT_DEPENDENCY"
