"""Which AI tests know their own answer (ticket 27-B).

A test whose expected values are the very values it wrote earlier (write X, read X; compare with
an ID an earlier answer gave) is right for any app that keeps data, so it needs no outside answer
key. A test that expects some other literal (a computed total, a status word) rests on somebody's
idea of what the app does; an AI must not supply that unchecked.
"""

from typing import Any, Literal

from cmp_testsuite.model import HttpStep, SuiteCase

Basis = Literal["PROPERTY", "FREE"]


def expectation_basis(case: SuiteCase) -> Basis:
    """PROPERTY when every expected value was sent by this case at or before the checking step."""
    if not case.has_value_assertion():
        return "FREE"
    sent: list[tuple[type, Any]] = []
    for step in case.steps:
        if not isinstance(step, HttpStep):
            continue
        sent.extend(_leaves(step.json_body))
        expect = step.expect
        wanted = list(expect.json_equals.values())
        if expect.body_contains is not None:
            wanted.append(expect.body_contains)
        if any((type(value), value) not in sent for value in wanted):
            return "FREE"
    return "PROPERTY"


def _leaves(value: Any) -> list[tuple[type, Any]]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaves(item)]
    if value is None:
        return []
    return [(type(value), value)]
