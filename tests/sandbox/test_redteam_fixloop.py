"""Ticket 36: Finding state across a fix/rebuild cycle, scoped to the exact image Digest (P-10).

Part 1 (this block): the state machine and the `classify_failure` extension. Nothing here talks
to a cluster or the Fixer; `RevalidationLedger` only records what a caller already decided by
actually re-running the Finding's own reproduction against the new image.
"""

from cmp_worker.failure_classifier import AttemptSignals, Classification, classify_failure
from cmp_worker.redteam_fixloop import RevalidationLedger

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


# --- the state machine ---------------------------------------------------------------------------


def test_a_finding_that_stops_reproducing_on_the_new_digest_is_fixed():
    ledger = RevalidationLedger()
    record = ledger.record("f-1", DIGEST_A, reproduces=False)
    assert record.status == "FIXED" and record.image_digest == DIGEST_A


def test_a_finding_that_still_reproduces_stays_open():
    ledger = RevalidationLedger()
    record = ledger.record("f-1", DIGEST_A, reproduces=True)
    assert record.status == "STILL_OPEN"


def test_a_finding_that_was_fixed_but_reproduces_again_on_a_later_digest_is_a_regression():
    ledger = RevalidationLedger()
    ledger.record("f-1", DIGEST_A, reproduces=False)
    record = ledger.record("f-1", DIGEST_B, reproduces=True)
    assert record.status == "REGRESSED"


def test_a_finding_that_stays_fixed_across_a_later_digest_stays_fixed():
    ledger = RevalidationLedger()
    ledger.record("f-1", DIGEST_A, reproduces=False)
    record = ledger.record("f-1", DIGEST_B, reproduces=False)
    assert record.status == "FIXED"


def test_an_unverified_finding_has_no_status_for_a_digest_it_was_never_checked_against():
    ledger = RevalidationLedger()
    ledger.record("f-1", DIGEST_A, reproduces=False)
    # P-10: a result found against digest A says nothing about digest B until it is re-run there.
    assert ledger.status_for("f-1", DIGEST_B) is None
    assert ledger.status_for("f-1", DIGEST_A) == "FIXED"


def test_findings_are_tracked_independently_of_each_other():
    ledger = RevalidationLedger()
    ledger.record("f-1", DIGEST_A, reproduces=False)
    ledger.record("f-2", DIGEST_A, reproduces=True)
    assert ledger.status_for("f-1", DIGEST_A) == "FIXED"
    assert ledger.status_for("f-2", DIGEST_A) == "STILL_OPEN"


# --- classify_failure gets a security category -------------------------------------------------


def test_open_redteam_findings_are_classified_as_security():
    result = classify_failure(AttemptSignals(open_security_findings=("f-1",)))
    assert result.failure_class == "SECURITY" and result.fixer_allowed is True
    assert result.reasons == ["FINDING:f-1"]


def test_an_infra_failure_still_wins_over_an_open_finding():
    result = classify_failure(
        AttemptSignals(sandbox_error="CLUSTER_COMMAND_FAILED", open_security_findings=("f-1",))
    )
    assert result.failure_class == "INFRA"


def test_no_open_findings_means_the_existing_classification_is_unaffected():
    result = classify_failure(AttemptSignals())
    assert result.failure_class == "SPEC" and isinstance(result, Classification)


# ==================================================================================================
# Part 2: the revalidation loop (one Finding, dispatched against the new Digest only) and the
# refusal of a fix that weakens an existing protection.
# ==================================================================================================

from datetime import UTC, datetime

from cmp_worker.redteam_fixloop import (
    SECURITY_LOWERED_BY_FIX,
    ProtectionSignals,
    revalidate,
    weakens_protection,
)
from cmp_worker.redteam_safeguards import Finding, Intensity, Severity

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def finding(**changes):
    fields = {
        "finding_id": "f-1", "category": "input-validation", "severity": Severity.HIGH,
        "endpoint": "/orders", "summary": "x", "repro_summary": "POST /orders with text quantity.",
        "image_digest": DIGEST_A, "suite_sha256": "b" * 64, "intensity": Intensity.PASSIVE,
        "executed_at": NOW,
    }  # fmt: skip
    return Finding(**(fields | changes))


def test_revalidate_dispatches_only_this_findings_own_reproduction(tmp_path=None):
    seen = []

    def reproduce(target_finding, image_digest):
        seen.append((target_finding.finding_id, image_digest))
        return False  # fixed

    ledger = RevalidationLedger()
    record = revalidate(finding(), DIGEST_B, reproduce=reproduce, ledger=ledger)
    assert record.status == "FIXED"
    assert seen == [("f-1", DIGEST_B)]  # never the old Digest, never another Finding


def test_revalidate_against_the_same_digest_the_finding_was_already_fixed_on_does_not_redispatch():
    calls = {"n": 0}

    def reproduce(target_finding, image_digest):
        calls["n"] += 1
        return False

    ledger = RevalidationLedger()
    revalidate(finding(), DIGEST_B, reproduce=reproduce, ledger=ledger)
    revalidate(finding(), DIGEST_B, reproduce=reproduce, ledger=ledger)
    assert calls["n"] == 1  # the second call already has a verified status for this Digest


# --- weakening a fix must refuse -----------------------------------------------------------------


def test_removing_an_authentication_check_is_refused():
    assert weakens_protection(ProtectionSignals(authentication_removed=True)) == [
        SECURITY_LOWERED_BY_FIX + ":authentication"
    ]


def test_widening_access_control_is_refused():
    assert weakens_protection(ProtectionSignals(access_control_widened=True)) == [
        SECURITY_LOWERED_BY_FIX + ":access_control"
    ]


def test_turning_off_encryption_is_refused():
    assert weakens_protection(ProtectionSignals(encryption_removed=True)) == [
        SECURITY_LOWERED_BY_FIX + ":encryption"
    ]


def test_an_ordinary_fix_that_touches_none_of_these_is_accepted():
    assert weakens_protection(ProtectionSignals()) == []


def test_several_weakenings_are_all_reported_not_just_the_first():
    codes = weakens_protection(
        ProtectionSignals(authentication_removed=True, encryption_removed=True)
    )
    assert len(codes) == 2


# ==================================================================================================
# Part 3: the attempt limit (separate from the ordinary Fixer loop) and the loop that ties a
# Finding, a fix, and revalidation together.
# ==================================================================================================

from cmp_worker.redteam_fixloop import MAX_SECURITY_FIX_ATTEMPTS, run_fix_loop
from cmp_worker.sandbox_events import MAX_ATTEMPTS as ORDINARY_MAX_ATTEMPTS


def test_the_security_fix_attempt_limit_is_its_own_constant_not_the_ordinary_one():
    # Two separate module-level constants: exhausting one never touches the other's count.
    assert isinstance(MAX_SECURITY_FIX_ATTEMPTS, int) and MAX_SECURITY_FIX_ATTEMPTS >= 1
    assert isinstance(ORDINARY_MAX_ATTEMPTS, int)


def test_a_fix_that_clears_the_finding_on_the_first_try_closes_the_loop():
    digests = iter([DIGEST_B])
    fixed = run_fix_loop(
        finding(),
        fix=lambda f, digest: (next(digests), ProtectionSignals()),
        reproduce=lambda f, digest: False,
        ledger=RevalidationLedger(),
    )
    assert fixed.status == "FIXED" and fixed.attempts == 1 and fixed.image_digest == DIGEST_B


def test_a_fix_that_never_clears_the_finding_stops_at_the_attempt_limit():
    digests = iter([f"sha256:{n:064d}" for n in range(1, MAX_SECURITY_FIX_ATTEMPTS + 1)])
    result = run_fix_loop(
        finding(),
        fix=lambda f, digest: (next(digests), ProtectionSignals()),
        reproduce=lambda f, digest: True,
        ledger=RevalidationLedger(),
    )
    assert result.status == "STILL_OPEN" and result.attempts == MAX_SECURITY_FIX_ATTEMPTS


def test_a_fix_that_weakens_a_protection_is_refused_before_it_is_ever_revalidated():
    calls = {"n": 0}

    def reproduce(f, digest):
        calls["n"] += 1
        return False

    result = run_fix_loop(
        finding(),
        fix=lambda f, digest: (DIGEST_B, ProtectionSignals(encryption_removed=True)),
        reproduce=reproduce,
        ledger=RevalidationLedger(),
    )
    assert result.status == "REJECTED_FOR_REVIEW"
    assert result.rejection_reasons == [f"{SECURITY_LOWERED_BY_FIX}:encryption"]
    assert calls["n"] == 0  # a weakening fix is never even tried against the executor


def test_a_finding_found_fixed_earlier_that_reproduces_again_on_a_fresh_run_is_a_regression():
    # The Finding was closed in an earlier fix loop (recorded on the shared ledger). A later,
    # separate run (triggered by something unrelated rebuilding the image) reproduces it again.
    ledger = RevalidationLedger()
    first = run_fix_loop(
        finding(), fix=lambda f, digest: (DIGEST_B, ProtectionSignals()),
        reproduce=lambda f, digest: False, ledger=ledger,
    )  # fmt: skip
    assert first.status == "FIXED"

    second = run_fix_loop(
        finding(), fix=lambda f, digest: ("sha256:" + "c" * 64, ProtectionSignals()),
        reproduce=lambda f, digest: True, ledger=ledger,
    )  # fmt: skip
    assert second.status == "REGRESSED" and second.attempts == 1
