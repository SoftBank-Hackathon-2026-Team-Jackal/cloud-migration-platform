"""Safeguards around a red-team run (ticket 34, ADR-008): what may start, and what it may leave.

This module decides nothing about *how* anything is tested. It holds the contract of what a run
reports (`Finding`), the rule that evidence is stored only after the app's own secret values and
secret-looking strings are gone from it, and (further down) the checks and the ordering that keep
a run from starting without approval, time, a baseline and a cut-off outside world.
"""

import base64
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import quote

from cmp_scanners.masking import MASK, mask_text
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cmp_worker.artifact_store import ArtifactRef, ArtifactStore
from cmp_worker.sandbox_deploy import SandboxError, SandboxPlan
from cmp_worker.sandbox_isolation import Executor

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


# --- before a run starts ------------------------------------------------------------------------

# What must still be left of the Sandbox's life after the budget: put the data back, bring the
# outside world back, remove the executor.
SAFETY_MARGIN_SECONDS = 120
NOT_APPROVED = "REDTEAM_NOT_APPROVED"
APPROVAL_MISMATCH = "REDTEAM_APPROVAL_MISMATCH"
APPROVAL_USED = "REDTEAM_APPROVAL_USED"
TIME_INSUFFICIENT = "REDTEAM_TIME_INSUFFICIENT"
BASELINE_MISSING = "REDTEAM_BASELINE_MISSING"


class RunConfig(BaseModel):
    """What will actually run. The approval must describe exactly this."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intensity: Intensity
    time_budget_seconds: int = Field(ge=30, le=3600)


class Approval(BaseModel):
    """A user's yes for one run. It names the run, the intensity and the budget it was given for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str = Field(min_length=1)
    approver: str = Field(min_length=1)
    approved_at: datetime
    run_id: str = Field(min_length=1)
    intensity: Intensity
    time_budget_seconds: int


class ApprovalLedger(Protocol):
    def consume(self, approval_id: str) -> bool:
        """True the first time an approval is used, False every time after. Must be atomic."""
        ...


class InMemoryApprovalLedger:
    """For tests and a single local worker. A deployment shared by several workers needs the
    same rule in the business database; the Protocol is the seam for that."""

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def consume(self, approval_id: str) -> bool:
        with self._lock:
            if approval_id in self._used:
                return False
            self._used.add(approval_id)
            return True


def approval_request(plan: SandboxPlan, config: RunConfig) -> dict[str, Any]:
    """What the approver is shown. Facts and names only: no values, no credentials."""
    return {
        "run_id": plan.run_id,
        "target": plan.namespace,
        "scope": "this Sandbox copy of the application only",
        "intensity": config.intensity.value,
        "time_budget_seconds": config.time_budget_seconds,
        "test_data_is_reset_afterwards": config.intensity is Intensity.ACTIVE,
        "outside_calls_are_cut_meanwhile": True,
    }


def check_start(
    plan: SandboxPlan,
    config: RunConfig,
    approval: Approval | None,
    *,
    now: datetime,
    baseline_exists: Callable[[], bool],
    ledger: ApprovalLedger,
) -> None:
    """Raise `SandboxError(code)` unless the run may start. Nothing is created here.

    The approval is used up last, so a start that is refused for any other reason leaves it
    usable. Time is judged now, not when the approval was given: waiting for a yes spends the
    Sandbox's life. A passive run never asks for the baseline.
    """
    if approval is None:
        raise SandboxError(NOT_APPROVED)
    if (
        approval.run_id != plan.run_id
        or approval.intensity is not config.intensity
        or approval.time_budget_seconds != config.time_budget_seconds
    ):
        raise SandboxError(APPROVAL_MISMATCH)
    remaining = plan.expires_at - now.timestamp()
    if remaining < config.time_budget_seconds + SAFETY_MARGIN_SECONDS:
        raise SandboxError(TIME_INSUFFICIENT)
    if config.intensity is Intensity.ACTIVE and not baseline_exists():
        raise SandboxError(BASELINE_MISSING)
    if not ledger.consume(approval.approval_id):
        raise SandboxError(APPROVAL_USED)


# --- the run itself: the order of things, and what always happens afterwards ---------------------

EXTERNAL_NOT_CUT = "REDTEAM_EXTERNAL_NOT_CUT"


class SandboxOps(Protocol):
    """The parts of `SandboxService` a run uses."""

    def external_cut(self, plan: SandboxPlan) -> AbstractContextManager[None]: ...

    def reset_test_data(self, plan: SandboxPlan) -> dict[str, str]: ...

    def baseline_exists(self, plan: SandboxPlan) -> bool: ...


class IsolationOps(Protocol):
    """`RedTeamIsolation` (ticket 33)."""

    def session(
        self, plan: SandboxPlan, *, target_resource_id: str
    ) -> AbstractContextManager[Executor]: ...


@dataclass(frozen=True)
class Run:
    """What the work inside a run is given: the executor and the one address it may use, how hard
    it may try, and the moment (epoch seconds) after which it must stop."""

    executor: Executor
    intensity: Intensity
    deadline: float


class RedTeamRunner:
    """Starts a run only when `check_start` allows it, and wraps the work in the safeguards.

    Order matters. Isolation is proved *first*: cutting the egress proxy before that would make
    the proxy look unreachable and the proof would say nothing about the policies. Then the
    outside world is cut. When the work ends, in any way, the cut is lifted and the executor
    removed, and only then is the data put back, so nothing is still talking to the application
    while it is restored. A passive run changes no data and resets nothing.
    """

    def __init__(
        self,
        service: SandboxOps,
        isolation: IsolationOps,
        ledger: ApprovalLedger,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._service = service
        self._isolation = isolation
        self._ledger = ledger
        self._clock = clock

    @contextmanager
    def run(
        self,
        plan: SandboxPlan,
        *,
        target_resource_id: str,
        config: RunConfig,
        approval: Approval | None,
    ) -> Iterator[Run]:
        now = self._clock()
        check_start(
            plan, config, approval, now=now,
            baseline_exists=lambda: self._service.baseline_exists(plan), ledger=self._ledger,
        )  # fmt: skip
        started = False
        try:
            with ExitStack() as stack:
                executor = stack.enter_context(
                    self._isolation.session(plan, target_resource_id=target_resource_id)
                )
                try:
                    stack.enter_context(self._service.external_cut(plan))
                except SandboxError:
                    raise SandboxError(EXTERNAL_NOT_CUT) from None
                started = True
                yield Run(
                    executor=executor,
                    intensity=config.intensity,
                    deadline=now.timestamp() + config.time_budget_seconds,
                )
        finally:
            # Not hidden if it fails: data that could not be put back is a result in itself.
            if started and config.intensity is Intensity.ACTIVE:
                self._service.reset_test_data(plan)
