"""`classify_failure`: a deterministic, ordered classification of one failed attempt (AV-015).

Order (plan 2.8): INFRA -> RENDER -> BUILD -> SPEC (blocked traffic) -> CODE -> SPEC. INFRA and
RENDER are not something the Fixer can repair, so the result says whether the Fixer may be called.
Inputs are codes and field names produced by earlier steps; no model output is trusted here.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_contracts.verification import Verdict

FailureClass = Literal["SPEC", "CODE", "BUILD", "RENDER", "INFRA", "SECURITY"]

# Sandbox environment failures that no change to the app or requirements can fix.
INFRA_SANDBOX_CODES = {"CLUSTER_COMMAND_FAILED", "PROXY_FAILED", "TEARDOWN_INCOMPLETE"}
# Pod events that mean the image could not be pulled (registry access or authentication).
INFRA_POD_EVENTS = {"ImagePullBackOff", "ErrImagePull", "RegistryUnavailable"}
# The rendered Sandbox does not match what the requirements asked for.
RENDER_SANDBOX_CODES = {"DB_VERSION_MISMATCH", "INVALID_SANDBOX_REQUEST"}
_BLOCKED = ("EGRESS_BLOCKED:", "MIGRATE_SOURCE_CALLED:")


@dataclass(frozen=True)
class AttemptSignals:
    sandbox_error: str | None = None  # SandboxError.code from prepare/apply/check
    pod_events: list[str] = field(default_factory=list)
    infra_timeout: bool = False  # Helm/Kubernetes API timeout
    app_cause_in_pod: bool = False  # pod events or logs show an application cause
    render_mismatches: list[str] = field(default_factory=list)
    build_failure_class: str | None = None  # from build_request.classify_failure
    verdict: Verdict | None = None
    stack_in_app_code: bool = False  # the failing stack trace points at application code
    # Ticket 36: ids of this run's red-team Findings still OPEN or STILL_OPEN. Empty unless a
    # red-team run happened, so every existing call site is unaffected by this field.
    open_security_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Classification:
    failure_class: FailureClass
    reasons: list[str]

    @property
    def fixer_allowed(self) -> bool:
        return self.failure_class not in {"INFRA", "RENDER"}


def classify_failure(signals: AttemptSignals) -> Classification:
    code = signals.sandbox_error
    # A command that failed while the pods show an application cause (readiness failing, crash
    # loop) is the application's problem, not the environment's.
    app_failed_to_start = code == "CLUSTER_COMMAND_FAILED" and signals.app_cause_in_pod
    # (0) The environment itself failed.
    if code in INFRA_SANDBOX_CODES and not app_failed_to_start:
        return Classification("INFRA", [f"SANDBOX:{code}"])
    pulls = sorted(set(signals.pod_events) & INFRA_POD_EVENTS)
    if pulls:
        return Classification("INFRA", [f"POD_EVENT:{e}" for e in pulls])
    if signals.build_failure_class == "INFRA":
        return Classification("INFRA", ["BUILD:INFRA"])
    if signals.infra_timeout and not signals.app_cause_in_pod and not app_failed_to_start:
        return Classification("INFRA", ["TIMEOUT_WITHOUT_APP_CAUSE"])
    # (0.5) A red-team Finding the app itself failed, not Sandbox plumbing around it, and not the
    # verdict's own test suite: an open Finding is reported on its own, ahead of CODE/SPEC, so the
    # Fixer gets the Finding's reproduction instead of guessing from unrelated verdict reasons.
    if signals.open_security_findings:
        reasons = [f"FINDING:{i}" for i in sorted(signals.open_security_findings)]
        return Classification("SECURITY", reasons)
    # (1) The render differs from the requirements field by field.
    if signals.render_mismatches or code in RENDER_SANDBOX_CODES:
        reasons = [f"RENDER_MISMATCH:{m}" for m in sorted(signals.render_mismatches)]
        return Classification("RENDER", reasons or [f"SANDBOX:{code}"])
    # (2) The build failed.
    if signals.build_failure_class == "BUILD":
        return Classification("BUILD", ["BUILD:FAILED"])
    verdict_reasons = list(signals.verdict.reasons) if signals.verdict else []
    # (3) Traffic the intent did not list: a missing intent, not a code bug.
    blocked = [r for r in verdict_reasons if r.startswith(_BLOCKED)]
    if blocked:
        return Classification("SPEC", blocked)
    # (4) A failing test whose stack points at application code.
    failed = [r for r in verdict_reasons if r.startswith("TEST_FAILED:")]
    health = code == "HEALTH_CHECK_FAILED" or app_failed_to_start
    if (failed or health) and signals.stack_in_app_code:
        return Classification("CODE", failed or [f"SANDBOX:{code}"])
    # (5) Everything else is treated as a specification problem.
    return Classification(
        "SPEC", failed or verdict_reasons or [f"SANDBOX:{code}" if code else "UNKNOWN"]
    )


def find_render_mismatches(requirements: RequirementsV2, request: dict[str, Any]) -> list[str]:
    """Field-level differences between the requirements and the request handed to the renderer."""
    rendered = {r["id"]: r for r in request.get("resources", [])}
    out: list[str] = []
    for want in requirements.resources:
        got = rendered.get(want.id)
        if got is None:
            out.append(f"{want.id}:missing")
            continue
        if want.kind == "APPLICATION":
            pairs = {
                "port": got.get("port"), "health_path": got.get("health_path"),
                "replicas": got.get("replicas"),
                "cpu_millicores": got.get("resources", {}).get("cpu_millicores"),
                "memory_mib": got.get("resources", {}).get("memory_mib"),
            }  # fmt: skip
        else:
            pairs = {
                "version": got.get("version"),
                "database.name": got.get("database", {}).get("name"),
                "database.username": got.get("database", {}).get("username"),
                "storage_gib": got.get("storage", {}).get("size_gib"),
                "cpu_millicores": got.get("resources", {}).get("cpu_millicores"),
                "memory_mib": got.get("resources", {}).get("memory_mib"),
            }  # fmt: skip
        expected = {
            "port": getattr(want, "port", None), "health_path": getattr(want, "health_path", None),
            "replicas": getattr(want, "replicas", None), "version": getattr(want, "version", None),
            "database.name": want.database.name if want.kind == "POSTGRESQL" else None,
            "database.username": want.database.username if want.kind == "POSTGRESQL" else None,
            "storage_gib": getattr(want, "storage_gib", None),
            "cpu_millicores": want.cpu_millicores, "memory_mib": want.memory_mib,
        }  # fmt: skip
        out += [f"{want.id}.{k}" for k, v in pairs.items() if v != expected[k]]
    out += [
        f"{rid}:unexpected" for rid in rendered if rid not in {r.id for r in requirements.resources}
    ]
    return sorted(out)
