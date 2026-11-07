"""원본·대상 서비스 건강 지표: 중계 서버 Envoy에서 측면별 5xx 비율과 p99를 계속 잰다.

트래픽 전환 워크플로의 관찰 창(`EnvoyObserver`)은 전환 중에만 돈다. 이 모듈은 전환 전·중·후 내내
일정 간격으로 Envoy 카운터를 읽어 직전 읽기와의 차이로 한 구간의 지표를 만든다. 원본(AWS)과 대상
(이전 환경)을 같은 기준으로 나란히 보이기 위해서다(Prometheus의 `rate()`와 같은 계산).

중계 서버에서 `python3 -m cmp_traffic.health`로 띄우면 플랫폼 `POST /system/traffic-health/report`로
보낸다. 숫자(요청·실패 수, ms)만 보내고 주소·헤더·본문은 읽지도 보내지도 않는다. 토큰은 로그에 쓰지 않는다.

환경변수:
  TRAFFIC_HEALTH_URL       보고 주소. 예: https://<플랫폼 호스트>/system/traffic-health/report
  PULSE_TOKEN              플랫폼 `PULSE_REPORT_TOKEN`과 같은 값(헤더 X-Pulse-Token)
  ENVOY_ADMIN_URL          기본 http://127.0.0.1:9901
  TRAFFIC_HEALTH_INTERVAL  구간 길이(초, 기본 5)

실행: python3 -m cmp_traffic.health            # 계속 보낸다
      python3 -m cmp_traffic.health --dry-run  # 한 구간을 재서 보낼 내용만 출력한다
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .envoy import _buckets, http_get_json, http_get_text, window_p99

SIDES = ("origin", "target")
COUNTER_STAT = r"cluster\.cluster_(origin|target)\.upstream_rq_(total|5xx|timeout)$"
BUCKET_FILTER = r"cluster\.cluster_(origin|target)\.upstream_rq_time"


@dataclass(frozen=True)
class Reading:
    """한 시점의 Envoy 누적값. 구간 지표는 두 Reading의 차이로 만든다."""

    at: float
    counters: dict[str, dict[str, float]]  # side -> {total, 5xx, timeout}
    buckets_text: str


def parse_counters(data: dict[str, Any]) -> dict[str, dict[str, float]]:
    """`/stats?format=json` 결과에서 측면별 누적 요청·5xx·타임아웃 수."""
    out: dict[str, dict[str, float]] = {side: {} for side in SIDES}
    for item in data.get("stats", []):
        name = item.get("name", "")
        for side in SIDES:
            prefix = f"cluster.cluster_{side}.upstream_rq_"
            if name.startswith(prefix):
                out[side][name[len(prefix) :]] = float(item.get("value") or 0)
    return out


def _delta(after: float, before: float) -> int:
    # Envoy가 다시 뜨면 카운터가 0부터 시작한다: 그 구간은 음수 대신 재시작 뒤의 값만 센다.
    return int(after - before) if after >= before else int(after)


def window(before: Reading, after: Reading, side: str) -> dict[str, Any]:
    """두 읽기 사이 한 측면의 지표. 요청이 없으면 비율·p99는 None(0으로 꾸미지 않는다)."""
    first, last = before.counters.get(side, {}), after.counters.get(side, {})
    requests = max(0, _delta(last.get("total", 0), first.get("total", 0)))
    errors_5xx = max(0, _delta(last.get("5xx", 0), first.get("5xx", 0)))
    timeouts = max(0, _delta(last.get("timeout", 0), first.get("timeout", 0)))
    failed = min(requests, errors_5xx + timeouts) if requests else errors_5xx + timeouts
    p99 = None
    if requests and _buckets(after.buckets_text, side):
        p99 = window_p99(before.buckets_text, after.buckets_text, side) or None
    seconds = max(0.001, after.at - before.at)
    return {
        "requests": requests,
        "errors_5xx": errors_5xx,
        "timeouts": timeouts,
        "failed": failed,
        "error_rate": round(failed / requests, 4) if requests else None,
        "rps": round(requests / seconds, 2),
        "p99_ms": p99,
    }


def report(before: Reading, after: Reading) -> dict[str, Any]:
    """보고 본문: 구간 길이와 측면별 지표."""
    return {
        "window_seconds": round(after.at - before.at, 2),
        **{side: window(before, after, side) for side in SIDES},
    }


class EnvoyReader:
    def __init__(
        self,
        admin_url: str = "http://127.0.0.1:9901",
        fetch: Callable[[str], dict[str, Any]] = http_get_json,
        fetch_text: Callable[[str], str] = http_get_text,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.admin_url = admin_url.rstrip("/")
        self.fetch, self.fetch_text, self.clock = fetch, fetch_text, clock

    def read(self) -> Reading:
        counters = self.fetch(
            f"{self.admin_url}/stats?"
            + urllib.parse.urlencode({"format": "json", "filter": COUNTER_STAT})
        )
        text = self.fetch_text(
            f"{self.admin_url}/stats/prometheus?"
            + urllib.parse.urlencode({"filter": BUCKET_FILTER})
        )
        return Reading(at=self.clock(), counters=parse_counters(counters), buckets_text=text)


def post(url: str, token: str, body: dict) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Pulse-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    url = os.environ.get("TRAFFIC_HEALTH_URL", "")
    token = os.environ.get("PULSE_TOKEN", "")
    interval = max(1.0, float(os.environ.get("TRAFFIC_HEALTH_INTERVAL", "5")))
    if not dry and not (url and token):
        print("TRAFFIC_HEALTH_URL과 PULSE_TOKEN이 필요합니다.", file=sys.stderr)
        return 2
    reader = EnvoyReader(os.environ.get("ENVOY_ADMIN_URL", "http://127.0.0.1:9901"))
    previous: Reading | None = None
    last_status: int | str | None = None
    while True:
        try:
            current = reader.read()
        except (OSError, ValueError) as exc:  # Envoy가 잠깐 없으면 다음 구간에 다시 읽는다
            if last_status != "envoy":
                print(f"envoy read failed: {type(exc).__name__}", file=sys.stderr, flush=True)
                last_status = "envoy"
            previous = None
            time.sleep(interval)
            continue
        if previous is not None:
            body = report(previous, current)
            if dry:
                print(json.dumps(body, ensure_ascii=False))
                return 0
            status = post(url, token, body)
            if status != last_status:  # 상태가 바뀔 때만 한 줄 남긴다
                print(f"report status {status}", flush=True)
                last_status = status
        previous = current
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
