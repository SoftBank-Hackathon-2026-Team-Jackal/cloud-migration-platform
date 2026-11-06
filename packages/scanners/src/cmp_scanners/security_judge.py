"""Judge the source's security against the target it is going to (ticket 29-A).

`measure_security` (ticket 28) says how each criterion stands at the source. `target_security`
says how it will stand at the target the platform builds, and `judge_security` compares the two:
LOWER (the move loses protection, the approval gate stops on it), SAME, HIGHER, or UNKNOWN (a
person has to look; never read as the same). A criterion that was weak at the source is marked
`weak_source` and gets a suggestion code for how the target could do better. Nothing here
applies a suggestion.

What the target is: the platform's own behaviour per provider, written down as data below so that
a change on the target side (for example TLS at the entry) is one line here and no change to the
judgement. Where the platform has no defined behaviour the state is UNKNOWN. These values mirror
what Provisioning and Traffic build today and need their confirmation.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from cmp_contracts.analysis import SecurityJudgement, SecurityMeasure
from cmp_contracts.provisioning_v2 import RequirementsV2

CRITERIA = (
    "EXTERNAL_HTTPS",
    "DB_ACCESS_SCOPE",
    "DB_TRANSPORT_ENCRYPTION",
    "ADMIN_API_EXPOSURE",
)
# What a target could do to be at least as protected; shown to the user, never applied here.
SUGGESTIONS = {
    "EXTERNAL_HTTPS": "ENFORCE_HTTPS_AT_ENTRY",
    "DB_ACCESS_SCOPE": "RESTRICT_DB_INGRESS_TO_APP",
    "DB_TRANSPORT_ENCRYPTION": "REQUIRE_DB_SSL",
    "ADMIN_API_EXPOSURE": "BLOCK_ADMIN_PATHS_AT_ENTRY",
}
# What can prove each criterion. A cluster-internal protection can be shown in the Sandbox; what
# crosses the external network (TLS at the entry, which paths the entry lets through) cannot, and
# is judged from what the target declares and measured again after the deploy.
PROOF = {
    "EXTERNAL_HTTPS": "POST_DEPLOY",
    "DB_ACCESS_SCOPE": "SANDBOX",
    "DB_TRANSPORT_ENCRYPTION": "SANDBOX",
    "ADMIN_API_EXPOSURE": "POST_DEPLOY",
}
_RANK = {"UNPROTECTED": 1, "PARTIAL": 2, "PROTECTED": 3}
_WEAK = {"UNPROTECTED", "PARTIAL"}


@dataclass(frozen=True)
class TargetSecurity:
    state: str
    reasons: list[str] = field(default_factory=list)


def _unknown(reason: str) -> TargetSecurity:
    return TargetSecurity("UNKNOWN", [reason])


def target_security(
    provider: str | None,
    requirements: RequirementsV2 | None,
    source_measures: Sequence[SecurityMeasure],
) -> dict[str, TargetSecurity]:
    """How each criterion will stand at the target. UNKNOWN where nothing is defined."""
    if provider is None:
        return {c: _unknown("TARGET_PROVIDER_UNKNOWN") for c in CRITERIA}
    has_database = requirements is not None and any(
        r.kind == "POSTGRESQL" for r in requirements.resources
    )
    states: dict[str, TargetSecurity] = {}

    # The entry. On-premises traffic comes in through the relay by address, with no domain or TLS
    # in the first phase (ADR-006). The cloud entries are not defined here yet.
    states["EXTERNAL_HTTPS"] = (
        TargetSecurity("UNPROTECTED", ["TARGET_ENTRY_WITHOUT_TLS"])
        if provider == "ON_PREM"
        else _unknown("TARGET_ENTRY_NOT_DEFINED")
    )

    # NetworkPolicies are made from the requirements' network intent and start from default deny
    # (ticket 26), so a database in the requirements is reachable only through listed edges.
    if requirements is None:
        states["DB_ACCESS_SCOPE"] = _unknown("REQUIREMENTS_MISSING")
    elif has_database:
        states["DB_ACCESS_SCOPE"] = TargetSecurity(
            "PROTECTED", ["TARGET_DEFAULT_DENY_NETWORK_POLICY"]
        )
    else:
        states["DB_ACCESS_SCOPE"] = _unknown("TARGET_DATABASE_NOT_IN_REQUIREMENTS")

    # The database chart has no SSL setup and the connection the platform builds has no sslmode.
    states["DB_TRANSPORT_ENCRYPTION"] = (
        TargetSecurity("NOT_APPLICABLE", ["TARGET_DATABASE_NOT_IN_REQUIREMENTS"])
        if requirements is not None and not has_database
        else TargetSecurity("UNPROTECTED", ["TARGET_DB_SSL_NOT_CONFIGURED"])
    )

    # The target entry forwards every path: it filters nothing, whatever the source did.
    admin = next((m for m in source_measures if m.criterion == "ADMIN_API_EXPOSURE"), None)
    if admin is None or admin.state == "UNKNOWN":
        states["ADMIN_API_EXPOSURE"] = _unknown("SOURCE_ADMIN_EXPOSURE_UNKNOWN")
    elif admin.state == "NOT_APPLICABLE":
        states["ADMIN_API_EXPOSURE"] = TargetSecurity("NOT_APPLICABLE")
    else:
        states["ADMIN_API_EXPOSURE"] = TargetSecurity(
            "UNPROTECTED", ["TARGET_ENTRY_HAS_NO_PATH_FILTER"]
        )
    return states


def _verdict(source: str, target: str) -> str:
    if source == "NOT_APPLICABLE" or target == "NOT_APPLICABLE":
        return "SAME"  # nothing at the source to lose, or nothing to protect at the target
    if source == "UNKNOWN" or target == "UNKNOWN":
        return "UNKNOWN"
    if _RANK[target] < _RANK[source]:
        return "LOWER"
    return "SAME" if _RANK[target] == _RANK[source] else "HIGHER"


def judge_security(
    measures: Sequence[SecurityMeasure], targets: Mapping[str, TargetSecurity]
) -> list[SecurityJudgement]:
    """One judgement per measured criterion, always in the same order."""
    by_criterion = {m.criterion: m for m in measures}
    judgements = []
    for criterion in CRITERIA:
        measure = by_criterion.get(criterion)
        if measure is None:
            continue
        target = targets.get(criterion) or _unknown("TARGET_NOT_DEFINED")
        verdict = _verdict(measure.state, target.state)
        weak = measure.state in _WEAK
        judgements.append(
            SecurityJudgement(
                criterion=criterion,  # type: ignore[arg-type]
                source_state=measure.state,
                target_state=target.state,  # type: ignore[arg-type]
                verdict=verdict,  # type: ignore[arg-type]
                weak_source=weak,
                reasons=sorted(set(target.reasons)),
                suggestion=SUGGESTIONS[criterion] if weak or verdict == "LOWER" else None,
            )
        )
    return judgements
