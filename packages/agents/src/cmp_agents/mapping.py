"""Deterministic parts of mapping (AV-007, AV-008): treatment table, row IDs, evidence checks.

The Mapper only proposes. Everything here is mechanical so the same input always gives the
same output and no model output can raise a row's verification status by itself.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Literal

from cmp_contracts.analysis import Evidence, MappingRow

Treatment = Literal["PROCEED", "APPROVAL", "BLOCK"]
# First-cycle scope: object storage stays at the source (S3 migration is a later phase).
FIRST_CYCLE_KEEP = {"object-storage"}
_CODE_LOCATOR = re.compile(r"^(?P<path>.+):(?P<line>\d+)$")


def compute_treatment(
    grade: str, required: bool | None, *, alternative_proven: bool = False
) -> Treatment | None:
    """Grade x required table. None until the user has confirmed `required`."""
    if required is None:
        return None
    if grade in {"DIRECT", "API_DIFF"}:
        return "PROCEED"
    if grade == "PARTIAL" and required and alternative_proven:
        return "PROCEED"
    return "BLOCK" if required else "APPROVAL"


def compute_treatments(
    rows: Sequence[MappingRow], required: Mapping[str, bool]
) -> list[MappingRow]:
    """Fill `required` and `treatment` from confirmed values; unconfirmed rows stay empty."""
    result = []
    for item in rows:
        confirmed = required.get(item.id)
        result.append(
            item.model_copy(
                update={
                    "required": confirmed,
                    "treatment": compute_treatment(item.grade, confirmed),
                }
            )
        )
    return result


def stabilize_row_ids(
    rows: Sequence[MappingRow], previous: Sequence[MappingRow]
) -> list[MappingRow]:
    """Keep a previous ID for the same (capability, usage); new usages get fresh unique IDs."""
    previous_ids = {(r.capability, r.usage): r.id for r in previous}
    reserved = set(previous_ids.values())
    assigned: list[str | None] = [None] * len(rows)
    used: set[str] = set()
    for index, candidate in enumerate(rows):
        old = previous_ids.get((candidate.capability, candidate.usage))
        if old is not None and old not in used:
            assigned[index] = old
            used.add(old)
    own = {r.id for r in rows}
    for index, candidate in enumerate(rows):
        if assigned[index] is not None:
            continue
        keep = candidate.id not in used and candidate.id not in reserved
        if keep:
            assigned[index] = candidate.id
            used.add(candidate.id)
    for index, candidate in enumerate(rows):
        if assigned[index] is not None:
            continue
        number = 1
        while (new := f"{candidate.capability}-{number}") in used | reserved | own:
            number += 1
        assigned[index] = new
        used.add(new)
    return [r.model_copy(update={"id": i}) for r, i in zip(rows, assigned, strict=True)]


def apply_dispositions(rows: Sequence[MappingRow]) -> list[MappingRow]:
    return [
        r.model_copy(update={"disposition": "KEEP"}) if r.capability in FIRST_CYCLE_KEEP else r
        for r in rows
    ]


def _file_line(workspace: Path, locator: str, *, line_required: bool) -> bool:
    match = _CODE_LOCATOR.match(locator)
    if match:
        raw, line = match.group("path"), int(match.group("line"))
    elif line_required:
        return False
    else:
        raw, line = locator, None
    root = workspace.resolve()
    target = (root / raw).resolve()
    if root not in target.parents or not target.is_file():
        return False
    if line is None:
        return True
    try:
        total = len(target.read_text().splitlines())
    except UnicodeDecodeError:
        return False
    return 1 <= line <= total


def _verify(
    evidence: Evidence,
    workspace: Path,
    manifest_locators: Collection[str],
    cloud_resource_ids: Collection[str],
) -> bool:
    if evidence.kind == "CODE":
        return _file_line(workspace, evidence.locator, line_required=True)
    if evidence.kind == "CONFIG":
        return _file_line(workspace, evidence.locator, line_required=False)
    if evidence.kind == "MANIFEST":
        return any(
            known == evidence.locator or known.startswith(evidence.locator + "/")
            for known in manifest_locators
        )
    return evidence.locator in cloud_resource_ids


def check_evidence(
    rows: Sequence[MappingRow],
    *,
    workspace: Path | str,
    manifest_locators: Collection[str],
    catalog_capabilities: Collection[str],
    cloud_resource_ids: Collection[str] = (),
) -> list[MappingRow]:
    """Mark each evidence item verified or not; only fully verified catalog rows are checked."""
    root = Path(workspace)
    checked = []
    for item in rows:
        evidence = [
            e.model_copy(
                update={"verified": _verify(e, root, manifest_locators, cloud_resource_ids)}
            )
            for e in item.evidence
        ]
        if item.status == "SANDBOX_VERIFIED":
            status = item.status
        elif (
            evidence
            and all(e.verified for e in evidence)
            and item.capability in catalog_capabilities
        ):
            status = "EVIDENCE_CHECKED"
        else:
            status = "UNVERIFIED"
        checked.append(item.model_copy(update={"evidence": evidence, "status": status}))
    return checked
