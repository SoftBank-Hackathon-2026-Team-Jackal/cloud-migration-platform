"""100% 전환 뒤 안정화 구간의 판정(순수 함수). 자동으로 되돌리지 않고 알릴지만 정한다."""

from __future__ import annotations

from typing import Any

from .contracts import Sample, TrafficPolicy
from .shifter import p99_limit


def stability_health(policy: TrafficPolicy, baseline: Sample, window: Sample) -> dict[str, Any]:
    """한 관찰 창의 대상 건강 상태. `breach`가 있으면 알려야 한다.

    요청이 `min_requests`보다 적으면 판단하지 않는다(오류율·p99를 꾸미지 않는다). 사유는 코드와 숫자만 담고
    문장은 웹이 고른다.
    """
    limit = p99_limit(policy, baseline)
    enough = window.requests >= policy.min_requests
    rate = round(window.failed / window.requests, 4) if window.requests else None
    breach: list[dict[str, Any]] = []
    if enough and rate is not None and rate > policy.alert_error_rate:
        breach.append({"code": "ERROR_RATE", "value": rate, "limit": policy.alert_error_rate})
    if enough and window.p99_ms > limit:
        breach.append({"code": "P99", "value": window.p99_ms, "limit": limit})
    return {
        "requests": window.requests,
        "failed": window.failed,
        "error_rate": rate,
        "p99_ms": window.p99_ms if window.requests else None,
        "baseline_p99_ms": baseline.p99_ms,
        "p99_limit_ms": limit,
        "alert_error_rate": policy.alert_error_rate,
        "judged": enough,
        "breach": breach,
    }
