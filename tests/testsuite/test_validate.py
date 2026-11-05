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
