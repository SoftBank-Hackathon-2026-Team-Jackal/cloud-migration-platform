"""A Fixer fix may not lower the security the analysis measured (ticket 29-B).

The Fixer's job is to make a failing app pass. One way to do that is to switch a protection off,
for example `sslmode=disable` so the app connects to a database that offers no SSL. These checks
run on a proposed fix before anything is written or stored. A refused fix ends the loop with the
reason and a person decides; the code is `SECURITY_LOWERED_BY_FIX:<criterion>`.

Only what can be seen is checked: plaintext `sslmode` literals in the files the fix writes, and the
judgements re-run against the fixed requirements. Pure functions, no I/O.
"""

from collections.abc import Iterable
from typing import Any

from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_scanners.security_judge import judge_security, target_security
from cmp_scanners.sslmode import PLAINTEXT_MODES, modes_in

_RANK = {"UNPROTECTED": 1, "PARTIAL": 2, "PROTECTED": 3}


def _code(criterion: str) -> str:
    return f"SECURITY_LOWERED_BY_FIX:{criterion}"


def _plaintext(text: str) -> set[str]:
    return {mode for mode in modes_in(text) if mode in PLAINTEXT_MODES}


def code_fix_lowers_security(report: Any, changes: Iterable[tuple[str, str, str]]) -> list[str]:
    """`changes` are (path, content before, content after). A plaintext `sslmode` that the file did
    not have is a lowering, unless the source was plaintext to begin with."""
    measures = {m.criterion: m for m in (getattr(report, "security", None) or [])}
    baseline = measures.get("DB_TRANSPORT_ENCRYPTION")
    if baseline is None or baseline.state in {"UNPROTECTED", "NOT_APPLICABLE"}:
        return []
    for _path, before, after in changes:
        if _plaintext(after) - _plaintext(before):
            return [_code("DB_TRANSPORT_ENCRYPTION")]
    return []


def _worse(before: Any, after: Any) -> bool:
    if after.verdict == "LOWER" and before.verdict != "LOWER":
        return True
    was, now = before.target_state, after.target_state
    if was in _RANK and now in _RANK and _RANK[now] < _RANK[was]:
        return True
    return was in _RANK and now == "UNKNOWN"  # a protection that can no longer be shown


def requirements_fix_lowers_security(report: Any, fixed: RequirementsV2) -> list[str]:
    """The judgements re-made against the fixed requirements; any that got worse."""
    judgements = getattr(report, "security_judgements", None)
    measures = getattr(report, "security", None)
    if not judgements or not measures:
        return []
    before = {j.criterion: j for j in judgements}
    after = judge_security(
        measures, target_security(getattr(report, "target_provider", None), fixed, measures)
    )
    return [
        _code(j.criterion)
        for j in after
        if j.criterion in before and _worse(before[j.criterion], j)
    ]
