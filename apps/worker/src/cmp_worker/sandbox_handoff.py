"""Read the Planning facts out of a returned Sandbox result (ticket 55, #190). Pure.

This is the receiving side only: the Sandbox result dict is produced by
`SandboxVerificationWorkflow` (analysis-verification) and is used as it is. Two facts are read:

- cleanup status: `cleanup_status` when the result carries one, else `cleanup_failed`, else
  CLEANED. Today a passing run whose teardown fails raises instead of returning, so a returned
  result without either field has been torn down (or never deployed).
- verified target: `target_selection_ref` when the result records the target the Sandbox verified.
  The Sandbox does not record one yet; then nothing is compared (agreed follow-up with the
  Sandbox owner).

A field that is present but malformed is refused with `SANDBOX_RESULT_INVALID`; nothing from the
result is put in the message.
"""

from dataclasses import dataclass
from typing import Literal, get_args

from cmp_contracts.provisioning import VersionRef
from pydantic import ValidationError

from cmp_worker.planning_handoff import HandoffError

CleanupStatus = Literal["CLEANED", "FAILED", "PENDING"]


@dataclass(frozen=True)
class SandboxOutcome:
    cleanup_status: CleanupStatus
    verified_target_ref: VersionRef | None


def sandbox_outcome(result: dict) -> SandboxOutcome:
    status = result.get("cleanup_status")
    failed = result.get("cleanup_failed", False)
    if (status is not None and status not in get_args(CleanupStatus)) or not isinstance(
        failed, bool
    ):
        raise HandoffError("SANDBOX_RESULT_INVALID")
    target = result.get("target_selection_ref")
    try:
        verified = None if target is None else VersionRef.model_validate(target)
    except ValidationError:
        raise HandoffError("SANDBOX_RESULT_INVALID") from None
    return SandboxOutcome(
        cleanup_status=status or ("FAILED" if failed else "CLEANED"),
        verified_target_ref=verified,
    )
