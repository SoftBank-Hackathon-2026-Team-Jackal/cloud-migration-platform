"""`validate_tests`: an AI test counts only if all four conditions hold (AV-012).

1. the expectation is vouched for: either it passes against the origin test resource (never the
   production origin), or it is a property test (every expected value is one the test wrote
   itself) that cites real source lines, or it was recorded from the app's own answers by the
   platform (`basis` RECORDED, never set by a model). An expectation that is none of these is refused: an AI's idea
   of some other value is not evidence (ticket 27-B). Citations are checked whenever a source is
   given, an origin run or not
2. asserts a result value, not just a status code
3. fails when the target's dependency is cut
4. the accepted set is pinned by SHA-256 (done by `pin_suite`)
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cmp_testsuite.basis import expectation_basis
from cmp_testsuite.bundle import dump_bundle
from cmp_testsuite.evidence import check_evidence
from cmp_testsuite.model import CaseResult, Suite, SuiteCase


class CaseRunner(Protocol):
    def __call__(self, case: SuiteCase) -> CaseResult: ...


@dataclass(frozen=True)
class Verdict:
    case_id: str
    accepted: bool
    reason: str | None  # code of the first failed condition


@dataclass
class Validation:
    verdicts: list[Verdict] = field(default_factory=list)
    accepted: list[SuiteCase] = field(default_factory=list)


def validate_tests(
    candidates: list[SuiteCase],
    *,
    target: CaseRunner,
    origin: CaseRunner | None,
    cut_dependency: Callable[[], AbstractContextManager[None]] | None,
    workspace: Path | str | None = None,
) -> Validation:
    out = Validation()
    for case in candidates:
        reason = _first_failure(case, target, origin, cut_dependency, workspace)
        out.verdicts.append(Verdict(case.id, reason is None, reason))
        if reason is None:
            out.accepted.append(case)
    return out


def _first_failure(case, target, origin, cut_dependency, workspace) -> str | None:
    if not case.has_value_assertion():
        return "NO_VALUE_ASSERTION"  # condition 2
    if workspace is not None:
        problem = check_evidence(workspace, case)
        if problem is not None:
            return problem  # condition 1: the citation does not hold
    if origin is not None:
        if origin(case).status != "PASSED":
            return "ORIGIN_FAILED"
    elif case.basis == "RECORDED":
        pass  # the platform took the expected values from the app's own answers
    elif expectation_basis(case) != "PROPERTY":
        return "ORIGIN_UNAVAILABLE"  # an outside expectation needs an outside answer
    elif workspace is None:
        return "EVIDENCE_UNAVAILABLE"  # a property test is vouched for by its citation
    if target(case).status != "PASSED":
        return "TARGET_FAILED"
    if cut_dependency is None:
        return "CUT_UNAVAILABLE"  # condition 3 cannot be checked
    with cut_dependency():
        if target(case).status == "PASSED":
            return "PASSES_WITHOUT_DEPENDENCY"
    return None


def pin_suite(
    name: str, standard: list[SuiteCase], accepted: list[SuiteCase]
) -> tuple[Suite, bytes]:
    """Condition 4: the final set and its bytes; the caller stores them and keeps the SHA-256."""
    suite = Suite(name=name, cases=[*standard, *accepted])
    return suite, dump_bundle(suite)
