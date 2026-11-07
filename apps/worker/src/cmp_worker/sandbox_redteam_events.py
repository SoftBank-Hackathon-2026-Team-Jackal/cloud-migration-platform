"""The `sandbox.redteam` event for the web rehearsal screen (pure; the Workflow emits it).

One event carries the whole current state of the red-team check, so the newest one is the one to
show. The user agreed once when the migration started; nothing waits for a person afterwards, so
there is no "waiting for approval" phase. A weakness is shown by its id, category, severity,
endpoint and two short sentences. The request that reproduces it and the evidence reference stay
in the artifact: they are never put in an event.
"""

from typing import Any

CONSENT_FIELDS = (
    "target",
    "scope",
    "intensity",
    "time_budget_seconds",
    "test_data_is_reset_afterwards",
    "outside_calls_are_cut_meanwhile",
)
FINDING_STATES = ("OPEN", "FIXED", "STILL_OPEN")


def consent_view(request: dict[str, Any] | None) -> dict[str, Any] | None:
    """The range the user agreed to, from the approval request (without the run id)."""
    if not request:
        return None
    return {name: request[name] for name in CONSENT_FIELDS}


def finding_view(finding: dict[str, Any], state: str) -> dict[str, Any]:
    if state not in FINDING_STATES:
        raise ValueError(f"unknown finding state: {state}")
    return {
        "id": finding["finding_id"],
        "category": finding["category"],
        "severity": finding["severity"],
        "endpoint": finding["endpoint"],
        "summary": finding["summary"],
        "repro_summary": finding["repro_summary"],
        "state": state,
    }


def redteam_data(
    phase: str,
    *,
    consent: dict[str, Any] | None,
    round_no: int,
    max_rounds: int,
    findings: list[dict[str, Any]],
    status: str | None = None,
    code: str | None = None,
    withheld: int = 0,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "consent": consent,
        "round": round_no,
        "max_rounds": max_rounds,
        "findings": findings,
        "status": status,
        "code": code,
        "withheld": withheld,
    }
