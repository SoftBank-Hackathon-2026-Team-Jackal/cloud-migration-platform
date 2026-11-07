"""플랫폼 서버의 맥박(`GET /system/pulse`): CPU·메모리 사용률과 최근 5분 오류 수.

웹 상단의 작은 위젯이 읽는다. 값은 이 서버(컨테이너가 보는 호스트)의 `/proc`에서 실제로 읽은 것만 낸다.
읽을 수 없으면(예: Windows 개발 PC) 그 값만 null이다. 꾸민 값은 내지 않는다. 퍼센트와 개수뿐이라 비밀값은 없다.
오류 수 = 최근 5분의 실패 이벤트(`status=failed`) + 오류 로그(`level=error`).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from cmp_contracts.events import utc_now
from sqlalchemy import func, select

from cmp_api.migration_events import MigrationEventRow, MigrationLogRow

WINDOW_SECONDS = 300
SAMPLE_GAP_SECONDS = 0.2


def parse_cpu(stat: str) -> tuple[int, int] | None:
    """`/proc/stat` 첫 줄에서 (바쁜 시간, 전체 시간). 읽을 수 없으면 None."""
    for line in stat.splitlines():
        if line.startswith("cpu "):
            try:
                values = [int(part) for part in line.split()[1:]]
            except ValueError:
                return None
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
            total = sum(values[:8])  # guest 시간은 user에 이미 들어 있다
            return total - idle, total
    return None


def cpu_percent(before: tuple[int, int] | None, after: tuple[int, int] | None) -> float | None:
    if before is None or after is None:
        return None
    total = after[1] - before[1]
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (after[0] - before[0]) / total)), 1)


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """(전체 MB, 쓰는 중 MB). MemAvailable 기준(캐시는 쓰는 중으로 보지 않는다)."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if key in ("MemTotal", "MemAvailable") and parts and parts[0].isdigit():
            values[key] = int(parts[0])  # kB
    if "MemTotal" not in values or "MemAvailable" not in values or values["MemTotal"] <= 0:
        return None
    total = values["MemTotal"]
    return total // 1024, (total - values["MemAvailable"]) // 1024


def parse_load(text: str) -> float | None:
    try:
        return round(float(text.split()[0]), 2)
    except (IndexError, ValueError):
        return None


def read_proc(name: str) -> str:
    try:
        with open(f"/proc/{name}", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def count_errors(sessions: Callable, now: datetime | None = None) -> int | None:
    """최근 5분 실패 이벤트 + 오류 로그. DB를 읽지 못하면 None(0으로 숨기지 않는다)."""
    since = ((now or datetime.now(UTC)) - timedelta(seconds=WINDOW_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    try:
        with sessions() as db:
            failed = db.scalar(
                select(func.count())
                .select_from(MigrationEventRow)
                .where(MigrationEventRow.status == "failed", MigrationEventRow.at >= since)
            )
            errors = db.scalar(
                select(func.count())
                .select_from(MigrationLogRow)
                .where(MigrationLogRow.level == "error", MigrationLogRow.at >= since)
            )
    except Exception:  # noqa: BLE001 - DB 장애는 위젯에서 "모름"으로 보이면 된다
        return None
    return int(failed or 0) + int(errors or 0)


def snapshot(
    sessions: Callable,
    read: Callable[[str], str] = read_proc,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """한 번 잰 맥박. CPU는 0.2초 간격의 두 번 읽기 차이로 구한다(블로킹이므로 스레드에서 부른다)."""
    before = parse_cpu(read("stat"))
    sleep(SAMPLE_GAP_SECONDS)
    after = parse_cpu(read("stat"))
    memory = parse_meminfo(read("meminfo"))
    return {
        "at": utc_now(),
        "cpu_percent": cpu_percent(before, after),
        "mem_percent": round(100.0 * memory[1] / memory[0], 1) if memory else None,
        "mem_used_mb": memory[1] if memory else None,
        "mem_total_mb": memory[0] if memory else None,
        "load1": parse_load(read("loadavg")),
        "cores": os.cpu_count(),
        "errors_5m": count_errors(sessions),
        "window_seconds": WINDOW_SECONDS,
    }
