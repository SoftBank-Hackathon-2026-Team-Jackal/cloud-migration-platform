"""Safeguards around a red-team run (ticket 34, ADR-008): what may start, and what it may leave.

This module decides nothing about *how* anything is tested. It holds the contract of what a run
reports (`Finding`), the rule that evidence is stored only after the app's own secret values and
secret-looking strings are gone from it, and (further down) the checks and the ordering that keep
a run from starting without approval, time, a baseline and a cut-off outside world.
"""

import base64
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import quote

from cmp_scanners.masking import MASK, mask_text
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cmp_worker.artifact_store import ArtifactRef, ArtifactStore

EVIDENCE_KIND = "redteam-evidence"
SECRET_IN_EVIDENCE = "SECRET_IN_EVIDENCE"
# Values shorter than this ("true", "8080") are ordinary configuration, and replacing them would
# wreck every response. A secret that short is not something exact replacement can protect.
MIN_SECRET_LENGTH = 6

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class Intensity(StrEnum):
    """PASSIVE only observes. ACTIVE may change the app's data and therefore needs a baseline.
    Anything that exhausts cluster resources is not an intensity: it is never run."""

    PASSIVE = "PASSIVE"
    ACTIVE = "ACTIVE"


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


_ORDER = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]


class Finding(BaseModel):
    """One result. Always tied to the exact image and test suite it was found against, so a
    fixed image never inherits an old verdict (P-10). Text fields are short on purpose: the
    request and response themselves live in the evidence artifact, masked, and never here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=80)
    severity: Severity
    endpoint: str = Field(min_length=1, max_length=200)
    summary: str = Field(max_length=300)
    repro_summary: str = Field(max_length=500)
    evidence_ref: ArtifactRef | None = None
    image_digest: ImageDigest
    suite_sha256: Sha256
    intensity: Intensity
    executed_at: datetime

    @field_validator("endpoint")
    @classmethod
    def _a_path_inside_the_app(cls, value: str) -> str:
        # A finding names where in the app something is, never which host: the executor has no
        # way to name a host, and a report must not suggest otherwise.
        if not value.startswith("/") or value.startswith("//") or "://" in value:
            raise ValueError("ENDPOINT_MUST_BE_A_PATH")
        return value


def make_finding(*, known_secrets: Iterable[str] = (), **fields: Any) -> Finding:
    """Build a Finding with the app's known secret values and any secret-looking strings removed
    from its free text. `mask_text` alone only catches some shapes (a `password=value` in the
    middle of a sentence passes it), so the values the Sandbox Secret holds are replaced first."""
    secrets = list(known_secrets)
    for name in ("summary", "repro_summary"):
        if name in fields:
            fields[name] = redact(fields[name], secrets)[0]
    return Finding(**fields)


class EvidenceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["STORED", "SECRET_IN_EVIDENCE"]
    ref: ArtifactRef | None = None


def _forms(secret: str) -> list[str]:
    """The ways a value commonly turns up in a captured request or response."""
    raw = secret.encode()
    forms = [
        secret,
        quote(secret, safe=""),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
    ]
    forms += [form.rstrip("=") for form in forms[2:]]
    return sorted({f for f in forms if f}, key=len, reverse=True)


def redact(evidence: str, known_secrets: Iterable[str]) -> tuple[str, list[str]]:
    """The evidence with known values and secret-looking strings replaced, and the forms to
    check it against afterwards."""
    forms = [f for s in known_secrets if len(s) >= MIN_SECRET_LENGTH for f in _forms(s)]
    text = evidence
    for form in sorted(set(forms), key=len, reverse=True):
        text = text.replace(form, MASK)
    return mask_text(text), forms


def store_evidence(
    store: ArtifactStore, *, finding_id: str, evidence: str, known_secrets: Iterable[str]
) -> EvidenceResult:
    """Store evidence only when nothing of the app's secrets is left in it.

    Exact forms are replaced first and then checked case-insensitively. If a value is still
    there (for example in upper case), the evidence is not stored at all: guessing at more
    transformations would only ever be a promise we cannot keep.
    """
    secrets = [s for s in known_secrets if s]
    text, forms = redact(evidence, secrets)
    lowered = text.casefold()
    if any(form.casefold() in lowered for form in forms):
        return EvidenceResult(status=SECRET_IN_EVIDENCE)
    ref = store.put(text.encode(), ref_id=f"{EVIDENCE_KIND}-{finding_id}", kind=EVIDENCE_KIND)
    return EvidenceResult(status="STORED", ref=ref)


class FindingSummary(BaseModel):
    """What the Workflow history keeps of a run: counts and ids, none of the text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int
    max_severity: Severity | None
    by_severity: dict[str, int]
    finding_ids: list[str]


def summarize(findings: Iterable[Finding]) -> FindingSummary:
    items = list(findings)
    counts = Counter(f.severity.value for f in items)
    top = max((f.severity for f in items), key=_ORDER.index, default=None)
    return FindingSummary(
        count=len(items),
        max_severity=top,
        by_severity=dict(counts),
        finding_ids=[f.finding_id for f in items],
    )
