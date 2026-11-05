"""`judge`: a pure, deterministic verdict from objective signals (AV-014).

Inputs are test results, the egress proxy's decisions, hosts of services being migrated away
from, and unsupported-call hits from application logs. The same inputs always give the same
reasons in the same order. Failure classification is a separate step (ticket 14).
"""

from cmp_contracts.analysis import MappingRow
from cmp_contracts.verification import Verdict
from cmp_testsuite.model import Suite, SuiteResult
from pydantic import BaseModel, ConfigDict

from cmp_worker.sandbox_network import EgressRecord


def _is_source(host: str, source_hosts: set[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in (h.lower() for h in source_hosts))


def judge(
    *,
    results: SuiteResult,
    egress: list[EgressRecord],
    source_hosts: set[str] | None = None,
    log_hits: list[str] | None = None,
) -> Verdict:
    reasons: list[str] = []
    if not any(r.status == "PASSED" for r in results.results):
        reasons.append("NO_TESTS_PASSED")
    reasons += [f"TEST_FAILED:{r.id}" for r in results.results if r.status == "FAILED"]
    for record in egress:
        if record.decision != "BLOCK":
            continue
        kind = (
            "MIGRATE_SOURCE_CALLED"
            if _is_source(record.host, source_hosts or set())
            else "EGRESS_BLOCKED"
        )
        reasons.append(f"{kind}:{record.host}:{record.port}")
    reasons += [f"UNSUPPORTED_CALL:{pattern}" for pattern in log_hits or []]
    ordered = sorted(set(reasons))
    return Verdict(passed=not ordered, reasons=ordered)


class Coverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    covered: dict[str, list[str]]  # capability -> test ids
    uncovered: list[str]  # migrated capabilities with no test that declares them


def coverage_report(rows: list[MappingRow], suite: Suite) -> Coverage:
    """Which migrated capabilities are exercised by at least one test (declared, not measured)."""
    migrated = sorted({r.capability for r in rows if r.disposition == "MIGRATE"})
    covered = {
        capability: sorted(c.id for c in suite.cases if capability in c.covers)
        for capability in migrated
    }
    return Coverage(
        covered={k: v for k, v in covered.items() if v},
        uncovered=[k for k, v in covered.items() if not v],
    )
