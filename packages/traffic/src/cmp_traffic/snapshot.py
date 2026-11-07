"""traffic.snapshot(#63)·traffic.prepare·traffic.ready(#234) 이벤트 페이로드 생성. 웹 계약과 필드를 맞춘다."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .contracts import Handover, Sample, TrafficPolicy


def build_snapshot(
    *,
    phase: str,
    policy: TrafficPolicy,
    weights: dict[str, int],
    step: int,
    baseline: Sample,
    observed: Sample | None,
    observe_left_seconds: float,
    decision: str | None = None,
    notes: list[dict[str, Any]] | None = None,
    health: dict[str, Any] | None = None,
    stabilize_left_seconds: float | None = None,
) -> dict[str, Any]:
    failed = observed.failed if observed else 0
    ratio = 0.0
    if observed and baseline.p99_ms > 0:
        ratio = round(observed.p99_ms / baseline.p99_ms, 2)
    snapshot: dict[str, Any] = {
        "phase": phase,
        "weights": dict(weights),
        "step": step,
        "gate": {
            "failed_requests": failed,
            "max_failed_requests": policy.max_failed_requests,
            "p99_ratio": ratio,
            "max_p99_ratio": policy.max_p99_ratio,
            "observe_left_seconds": observe_left_seconds,
        },
        # 원본 쪽에 남은 요청(전환이 끝나면 0으로 수렴해야 한다)
        "residual": {"requests_per_sec": 0, "streams": 0},
    }
    if decision:
        snapshot["decision"] = decision
    if notes:
        snapshot["notes"] = notes
    if health is not None:  # 안정화 구간(100% 뒤)의 대상 건강 상태
        snapshot["health"] = health
    if stabilize_left_seconds is not None:
        snapshot["stabilize_left_seconds"] = stabilize_left_seconds
    return snapshot


# ---- #234: traffic.prepare · traffic.ready (web `TrafficPrepareData` · `TrafficReadyData`) ----

# Activity failure type -> (check that failed, reason code). Codes only: the web picks the words.
PREPARE_FAILURES: dict[str, tuple[str, str]] = {
    "HandoverNotReady": ("external_endpoint", "HANDOVER_NOT_READY"),
    "RelayNotConfigured": ("external_endpoint", "RELAY_NOT_CONFIGURED"),
    "TargetMismatch": ("external_endpoint", "TARGET_MISMATCH"),
    "TargetUnhealthy": ("health", "TARGET_UNHEALTHY"),
}


def handover_checks(handover: Handover) -> list[dict[str, Any]]:
    """The two checks the Workflow can answer from the handover alone (no I/O)."""
    return [
        {
            "id": "handoff_status",
            "state": "done" if handover.status == "READY" else "failed",
            "value": handover.status,
        },
        {
            "id": "ready",
            "state": "done" if handover.ready else "failed",
            "value": "true" if handover.ready else "false",
        },
    ]


def build_prepare(
    *,
    state: str,
    handover: Handover,
    policy: TrafficPolicy,
    checks: list[dict[str, Any]],
    reason: str | None = None,
) -> dict[str, Any]:
    """`traffic.prepare` data: preflight checks plus the connection values the screen draws.

    Only values the platform knows are sent (relay kind, target endpoint, policy); no secrets.
    """
    data: dict[str, Any] = {
        "state": state,
        "checks": checks,
        "relay": {"name": "Envoy", "mode": "weighted_clusters"},
        "policy": {
            "steps": list(policy.steps),
            "observe_seconds": policy.observe_seconds,
            "max_failed_requests": policy.max_failed_requests,
            "max_p99_ratio": policy.max_p99_ratio,
        },
    }
    endpoint = handover.external_endpoint
    if endpoint:
        host = urlparse(endpoint if "//" in endpoint else f"//{endpoint}").hostname
        data["target"] = {"name": host or endpoint, "external_endpoint": endpoint}
    if reason:
        data["reason"] = reason
    return data


def prepare_failure(error_type: str | None, handover: Handover) -> tuple[list[dict[str, Any]], str]:
    """Checks and reason code for a failed `traffic_prepare` (unknown failures fail the endpoint
    check, the one the screen shows as running while the Activity works)."""
    failed_id, reason = PREPARE_FAILURES.get(
        error_type or "", ("external_endpoint", "PREPARE_FAILED")
    )
    checks = handover_checks(handover)
    if failed_id == "health":
        checks.append(endpoint_check(handover, "done"))
    checks.append({"id": failed_id, "state": "failed"})
    return checks, reason


def endpoint_check(handover: Handover, state: str) -> dict[str, Any]:
    check: dict[str, Any] = {"id": "external_endpoint", "state": state}
    if state == "done" and handover.external_endpoint:
        check["value"] = handover.external_endpoint
    return check


def build_ready(state: str, readiness: dict[str, Any]) -> dict[str, Any] | None:
    """`traffic.ready` data from the data migration's latest samples; None when a value is unknown
    (the web then keeps its replication fallback instead of showing a made-up 0)."""
    values = {key: readiness.get(key) for key in ("lag_ms", "longest_tx_ms", "commits_per_sec")}
    if any(not isinstance(value, int | float) or value < 0 for value in values.values()):
        return None
    return {"state": state, **values}
