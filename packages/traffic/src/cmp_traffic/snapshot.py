"""traffic.snapshot 이벤트 페이로드 생성(#63). 프론트 계약(TrafficSnapshotData)과 필드를 맞춘다."""

from __future__ import annotations

from typing import Any

from .contracts import Sample, TrafficPolicy


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
    return snapshot
