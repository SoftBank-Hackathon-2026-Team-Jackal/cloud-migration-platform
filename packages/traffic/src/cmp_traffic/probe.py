"""사용자 트래픽이 없을 때의 합성 요청 판정(순수 함수). 요청을 보내는 I/O는 Activity가 한다.

사용자가 없으면 지킬 사용자도 없으니 "요청이 부족하다"며 롤백하지 않는다. 대신 대상으로 직접 합성 요청을
보내 실패와 p99를 재고, 그 값으로 평소와 같은 판정(`evaluate_step`)을 한다. 합성 요청이 통과해야 진행하므로
검증 없이 올리는 것은 아니다. 원본에 사용자 요청이 있는데 대상만 0건이면(가중치가 안 먹은 것) 사용자 없음이
아니므로 이 경로를 타지 않는다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .contracts import Sample

# 한 번에 보내는 합성 요청의 상한(대상에 부담을 주지 않도록).
MAX_PROBE_REQUESTS = 200


def percentile(values: Sequence[float], q: float) -> float:
    """가장 가까운 순위(nearest-rank) 백분위. 값이 없으면 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def is_idle(origin_requests: int | None, target_requests: int, min_requests: int) -> bool:
    """이 단계에서 지금까지 원본·대상 모두 최소 요청 수에 못 미쳤는가(= 사용자 트래픽이 없다).

    원본 요청 수를 알 수 없으면(옛 관측 결과) 없다고 단정하지 않는다: 모르면 예전처럼 보수적으로 처리한다.
    """
    if origin_requests is None:
        return False
    return origin_requests + target_requests < min_requests


def probe_count(target_requests: int, min_requests: int) -> int:
    """판정에 필요한 요청 수를 채우려고 보낼 합성 요청 수(1 이상, 상한 있음)."""
    return max(1, min(MAX_PROBE_REQUESTS, min_requests - target_requests))


def summarize_probe(results: Sequence[tuple[int | None, float]]) -> Sample:
    """(HTTP 상태 또는 None, 응답 ms) 목록을 Sample로. 연결 실패·5xx는 실패로 센다(Envoy와 같은 기준)."""
    failed = sum(1 for status, _ in results if status is None or status >= 500)
    return Sample(
        requests=len(results),
        failed=failed,
        p99_ms=percentile([ms for _, ms in results], 99),
    )
