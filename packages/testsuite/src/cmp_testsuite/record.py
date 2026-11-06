"""Recorded answers as expected values (ticket 27-B).

The Tester proposes a `Probe`: a read-only request and which answer fields change from call to
call. The platform asks the app the same question twice and keeps what both answers agree on as
the expected values. A model never writes an expected value for these tests, so it cannot copy a
bug it read in the code or guess a value wrongly.

What this vouches for: the app still answers this way after later changes (a code fix). It does not
say the answer is right, nor that it matches the original app.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field

from cmp_testsuite.model import Evidence, Expect, HttpStep, SuiteCase, _Strict

Fetch = Callable[[str], tuple[int, bytes]]

# A suite runs its cases in id order. Recorded reads must run before any case that writes, on the
# data they were recorded against; "0" sorts before every letter, so they come first.
RECORDED_PREFIX = "ai-0rec-"

_MAX_LEAVES = 100
_Scalar = (str, int, float, bool, type(None))


class Probe(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=60)]
    description: str = ""
    path: Annotated[str, Field(pattern=r"^/[^\s{}]*$")]  # a plain path: nothing to fill in
    # dotted paths in the answer that differ from call to call (times, generated IDs, tokens)
    volatile: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)


@dataclass(frozen=True)
class Recorded:
    case: SuiteCase | None
    reason: str | None  # a code, never answer content


def record_probe(
    probe: Probe, fetch: Fetch, *, sensitive: Callable[[str], bool] | None = None
) -> Recorded:
    try:
        first, second = fetch(probe.path), fetch(probe.path)
    except OSError:
        return Recorded(None, "RECORD_UNREACHABLE")
    (status_a, body_a), (status_b, body_b) = first, second
    if status_a != status_b or not 200 <= status_a < 300:
        return Recorded(None, f"RECORD_STATUS {status_a}/{status_b}")
    try:
        document_a, document_b = json.loads(body_a), json.loads(body_b)
    except ValueError:
        return Recorded(None, "RECORD_NOT_JSON")
    try:
        leaves_a = _leaves(document_a, probe.volatile)
        leaves_b = _leaves(document_b, probe.volatile)
    except _UnsupportedKey:
        return Recorded(None, "RECORD_UNSUPPORTED_KEY")
    for path in sorted(set(leaves_a) | set(leaves_b)):
        if leaves_a.get(path, _MISSING) != leaves_b.get(path, _MISSING):
            return Recorded(None, f"RECORD_UNSTABLE {path}")
    if not leaves_a:
        return Recorded(None, "RECORD_EMPTY")
    if len(leaves_a) > _MAX_LEAVES:
        return Recorded(None, "RECORD_TOO_LARGE")
    if sensitive and any(isinstance(v, str) and sensitive(v) for v in leaves_a.values()):
        return Recorded(None, "RECORD_SENSITIVE")
    case = SuiteCase(
        id=f"{RECORDED_PREFIX}{probe.id}",
        description=probe.description or f"The answer of GET {probe.path} stays as recorded.",
        source="AI",
        effect="READ_ONLY",
        evidence=probe.evidence,
        basis="RECORDED",
        steps=[
            HttpStep(path=probe.path, expect=Expect(status=status_a, json_equals=leaves_a)),
        ],
    )
    return Recorded(case, None)


_MISSING = object()


class _UnsupportedKey(Exception):
    pass


def _leaves(document: Any, volatile: list[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}

    def walk(value: Any, path: str) -> None:
        if path and any(path == v or path.startswith(v + ".") for v in volatile):
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if "." in key:
                    raise _UnsupportedKey
                walk(item, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}.{index}" if path else str(index))
        elif isinstance(value, _Scalar) and path:
            found[path] = value

    walk(document, "")
    return found
