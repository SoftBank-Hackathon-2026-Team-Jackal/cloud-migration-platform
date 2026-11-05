"""`validate_tests`: an AI test counts only if all four conditions hold (AV-012).

1. passes against the origin test resource (never the production origin)
2. asserts a result value, not just a status code
3. fails when the target's dependency is cut
4. the accepted set is pinned by SHA-256 (done by `pin_suite`)
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from cmp_testsuite.bundle import dump_bundle
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
) -> Validation:
    out = Validation()
    for case in candidates:
        reason = _first_failure(case, target, origin, cut_dependency)
        out.verdicts.append(Verdict(case.id, reason is None, reason))
        if reason is None:
            out.accepted.append(case)
    return out


def _first_failure(case, target, origin, cut_dependency) -> str | None:
    if not case.has_value_assertion():
        return "NO_VALUE_ASSERTION"  # condition 2
    if origin is None:
        return "ORIGIN_UNAVAILABLE"  # condition 1 cannot be checked
    if origin(case).status != "PASSED":
        return "ORIGIN_FAILED"
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
