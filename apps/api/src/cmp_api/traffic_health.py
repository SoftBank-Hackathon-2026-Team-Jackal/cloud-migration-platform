"""원본·대상 서비스 건강 지표(`/system/traffic-health`): 중계 서버가 보낸 구간별 5xx 비율과 p99.

중계 서버의 `python3 -m cmp_traffic.health`가 몇 초마다 한 구간씩 보낸다. 값은 메모리에만 두고(재시작하면
비워진다) 최근 `MAX_POINTS`개만 남긴다. 마지막 보고가 `STALE_SECONDS`보다 오래되면 최신값을 "모름"(null)으로
내준다: 꾸민 값이나 지난 값을 지금 값처럼 보이지 않는다. 요청·실패 수와 ms뿐이라 비밀값은 없다.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from cmp_contracts.events import utc_now

SIDES = ("origin", "target")
MAX_POINTS = 180  # 5초 간격이면 15분
STALE_SECONDS = 20
SUMMARY_POINTS = 6  # 요약은 최근 6구간(약 30초)을 합쳐서 낸다: 한 구간의 튐을 줄인다


class HealthStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._points: deque[tuple[float, dict]] = deque(maxlen=MAX_POINTS)

    def put(self, body: dict) -> None:
        point = {"at": utc_now(), **body}
        with self._lock:
            self._points.append((self._clock(), point))

    def clear(self) -> None:
        with self._lock:
            self._points.clear()

    def view(self) -> dict:
        now = self._clock()
        with self._lock:
            points = list(self._points)
        fresh = bool(points) and now - points[-1][0] <= STALE_SECONDS
        series = [point for _, point in points]
        recent = series[-SUMMARY_POINTS:] if fresh else []
        return {
            "at": utc_now(),
            "fresh": fresh,
            "age_seconds": round(now - points[-1][0], 1) if points else None,
            "summary": {side: summarize(recent, side) for side in SIDES},
            "series": series,
        }


def summarize(points: list[dict], side: str) -> dict:
    """최근 구간들을 합친 요약. 오류율은 요청 수로 가중하고, p99는 구간 p99의 최댓값(보수적)이다."""
    windows = [p[side] for p in points]
    requests = sum(w["requests"] for w in windows)
    failed = sum(w["failed"] for w in windows)
    seconds = sum(p["window_seconds"] for p in points)
    p99s = [w["p99_ms"] for w in windows if w.get("p99_ms") is not None]
    return {
        "requests": requests,
        "failed": failed,
        "error_rate": round(failed / requests, 4) if requests else None,
        "p99_ms": max(p99s) if p99s else None,
        "rps": round(requests / seconds, 2) if seconds else None,
    }


STORE = HealthStore()
