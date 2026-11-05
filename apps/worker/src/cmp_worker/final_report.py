"""Final analysis version at Sandbox pass time (AV-017): mapping rows after the loop.

Everything here is mechanical. Rows change handling only through `RowChange` proposals, a row is
`SANDBOX_VERIFIED` only when a test that declares its capability passed and its evidence was
already checked, and the version id is a hash of the content, so the same content gives the same
reference.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence

from cmp_agents.mapping import compute_treatments
from cmp_contracts.analysis import AnalysisReport, MappingRow
from cmp_contracts.provisioning import VersionRef
from cmp_testsuite import Suite


class FinalReportError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def apply_row_changes(rows: Sequence[MappingRow], changes: Mapping[str, str]) -> list[MappingRow]:
    known = {r.id for r in rows}
    if set(changes) - known:
        raise FinalReportError("UNKNOWN_ROW")
    return [
        r.model_copy(update={"disposition": changes[r.id]}) if r.id in changes else r for r in rows
    ]


def with_decisions(rows: Sequence[MappingRow], required: Mapping[str, bool]) -> list[MappingRow]:
    """Recompute `required` and `treatment` from the confirmed values of the first review."""
    return compute_treatments(rows, required)


def changed_rows(previous: Sequence[MappingRow], final: Sequence[MappingRow]) -> list[MappingRow]:
    """Rows whose handling differs from the analysis version a person approved, by row ID."""
    before = {r.id: (r.disposition, r.treatment, r.required) for r in previous}
    return [r for r in final if before.get(r.id) != (r.disposition, r.treatment, r.required)]


def mark_verified(
    rows: Sequence[MappingRow], suite: Suite, results: Sequence[Mapping[str, str]]
) -> list[MappingRow]:
    """Raise rows to SANDBOX_VERIFIED when a passed test declares their capability."""
    passed = {r["id"] for r in results if r["status"] == "PASSED"}
    covered = {cap for case in suite.cases if case.id in passed for cap in case.covers}
    return [
        r.model_copy(update={"status": "SANDBOX_VERIFIED"})
        if r.status == "EVIDENCE_CHECKED" and r.capability in covered
        else r
        for r in rows
    ]


def final_version(
    previous: VersionRef, requirements_sha256: str, rows: Sequence[MappingRow]
) -> VersionRef:
    digest = hashlib.sha256(
        json.dumps(
            [previous.version, requirements_sha256, [r.model_dump(mode="json") for r in rows]],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:12]
    return VersionRef(id=previous.id, version=digest)


def build_final_report(
    previous: AnalysisReport, rows: Sequence[MappingRow], requirements_sha256: str
) -> AnalysisReport:
    return previous.model_copy(
        update={
            "ref": final_version(previous.ref, requirements_sha256, rows),
            "mapping": list(rows),
            "usage_profile": previous.usage_profile.model_copy(
                update={"usages": sorted(f"{r.capability}: {r.usage}" for r in rows)}
            ),
        }
    )
