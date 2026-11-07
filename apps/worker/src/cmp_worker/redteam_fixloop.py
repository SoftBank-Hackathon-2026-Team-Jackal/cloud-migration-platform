"""Finding state across a fix/rebuild cycle (ticket 36, ADR-008).

A Finding does not carry its own state (ticket 34's `Finding` is a point-in-time report). What
changes over a fix loop is tracked here, keyed by the finding and the exact image Digest it was
last checked against: a result found against one Digest says nothing about another until it is
re-run there (P-10 — the Digest Sandbox verified is the one Digest that may be deployed).
"""

from dataclasses import dataclass
from typing import Literal

FindingStatus = Literal["OPEN", "FIXED", "STILL_OPEN", "REGRESSED"]


@dataclass(frozen=True)
class FindingRecord:
    finding_id: str
    image_digest: str
    status: FindingStatus


class RevalidationLedger:
    """Record what re-running a Finding's own reproduction against a new image found.

    This never re-runs anything itself — `record` takes the outcome of that re-run (`reproduces`)
    from the caller, who is the only one in a position to have actually dispatched the Finding's
    stored reproduction against the new Digest's executor. All this does is turn that one outcome,
    plus whatever this Finding's last recorded status was, into the next status.
    """

    def __init__(self) -> None:
        self._records: dict[str, FindingRecord] = {}

    def record(self, finding_id: str, image_digest: str, *, reproduces: bool) -> FindingRecord:
        previous = self._records.get(finding_id)
        if reproduces:
            status: FindingStatus = (
                "REGRESSED" if previous and previous.status == "FIXED" else "STILL_OPEN"
            )
        else:
            status = "FIXED"
        record = FindingRecord(finding_id=finding_id, image_digest=image_digest, status=status)
        self._records[finding_id] = record
        return record

    def status_for(self, finding_id: str, image_digest: str) -> FindingStatus | None:
        """The status, but only if it was found against exactly this Digest."""
        record = self._records.get(finding_id)
        return record.status if record and record.image_digest == image_digest else None


# --- the revalidation loop ------------------------------------------------------------------------

from collections.abc import Callable
from dataclasses import dataclass as _dataclass

from cmp_worker.redteam_safeguards import Finding


def revalidate(
    finding: Finding,
    image_digest: str,
    *,
    reproduce: Callable[[Finding, str], bool],
    ledger: RevalidationLedger,
) -> FindingRecord:
    """Re-run exactly this Finding's own reproduction against exactly this Digest, and record the
    result. If this Finding already has a verified status for this Digest, nothing is dispatched
    again: the earlier check already answered the only question a repeat would ask."""
    existing = ledger.status_for(finding.finding_id, image_digest)
    if existing is not None:
        return FindingRecord(
            finding_id=finding.finding_id, image_digest=image_digest, status=existing
        )
    reproduces = reproduce(finding, image_digest)
    return ledger.record(finding.finding_id, image_digest, reproduces=reproduces)


# --- a fix may not weaken an existing protection ---------------------------------------------------

SECURITY_LOWERED_BY_FIX = "SECURITY_LOWERED_BY_FIX"


@_dataclass(frozen=True)
class ProtectionSignals:
    """What a proposed fix touched, as the caller determined it (a diff inspection or the Fixer's
    own declared changes) — this module judges only these flags, never source text itself."""

    authentication_removed: bool = False
    access_control_widened: bool = False
    encryption_removed: bool = False


def weakens_protection(signals: ProtectionSignals) -> list[str]:
    """Every protection a fix removed, as `SECURITY_LOWERED_BY_FIX:<name>` codes. Empty means the
    fix may proceed; any code means it is refused and a person decides (P-12)."""
    checks = (
        (signals.authentication_removed, "authentication"),
        (signals.access_control_widened, "access_control"),
        (signals.encryption_removed, "encryption"),
    )
    return [f"{SECURITY_LOWERED_BY_FIX}:{name}" for lowered, name in checks if lowered]


# --- the attempt limit and the loop ---------------------------------------------------------------

# R4 (ADR-008): a security fix gets its own budget, separate from `sandbox_events.MAX_ATTEMPTS`
# (the ordinary functional fix loop). Exhausting one never counts against the other.
MAX_SECURITY_FIX_ATTEMPTS = 3
REJECTED_FOR_REVIEW = "REJECTED_FOR_REVIEW"


@_dataclass(frozen=True)
class FixLoopResult:
    finding_id: str
    status: FindingStatus | Literal["REJECTED_FOR_REVIEW"]
    attempts: int
    image_digest: str | None = None
    rejection_reasons: list[str] | None = None


def run_fix_loop(
    finding: Finding,
    *,
    fix: Callable[[Finding, str | None], tuple[str, ProtectionSignals]],
    reproduce: Callable[[Finding, str], bool],
    ledger: RevalidationLedger,
    max_attempts: int = MAX_SECURITY_FIX_ATTEMPTS,
) -> FixLoopResult:
    """One Finding, end to end: ask for a fix, refuse it outright if it weakens a protection,
    otherwise revalidate against the new Digest it produced. Stops as soon as the Finding is no
    longer open, on a regression, on a refused fix, or once the attempt budget runs out —
    whichever comes first — and always reports which of those it was.
    """
    digest: str | None = finding.image_digest
    for attempt in range(1, max_attempts + 1):
        new_digest, signals = fix(finding, digest)
        reasons = weakens_protection(signals)
        if reasons:
            return FixLoopResult(
                finding_id=finding.finding_id, status=REJECTED_FOR_REVIEW, attempts=attempt,
                image_digest=new_digest, rejection_reasons=reasons,
            )  # fmt: skip
        record = revalidate(finding, new_digest, reproduce=reproduce, ledger=ledger)
        digest = new_digest
        if record.status in ("FIXED", "REGRESSED"):
            return FixLoopResult(
                finding_id=finding.finding_id, status=record.status, attempts=attempt,
                image_digest=new_digest,
            )  # fmt: skip
    return FixLoopResult(
        finding_id=finding.finding_id,
        status="STILL_OPEN",
        attempts=max_attempts,
        image_digest=digest,
    )
