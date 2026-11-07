"""Agent가 터널 상태 보고에 얹는 노드 CPU·메모리 사용률(웹 위젯의 이전 서버 값).

표준 라이브러리만 쓰고 `/proc/stat`·`/proc/meminfo`에서 숫자만 읽는다(프로세스·주소·파일은 읽지 않는다).
컨테이너 안의 /proc은 노드(맥 Docker Desktop이면 그 VM) 값이라 "이전 서버에 배정된 만큼"이 보인다.
CPU는 직전 읽기와의 차이라서 첫 호출은 값이 없다(None). 읽을 수 없으면 None이고 보고에서 빠진다.
"""

from __future__ import annotations

from pathlib import Path


def parse_cpu(stat: str) -> tuple[int, int] | None:
    """`/proc/stat` 첫 줄에서 (바쁜 시간, 전체 시간)."""
    for line in stat.splitlines():
        if line.startswith("cpu "):
            try:
                values = [int(part) for part in line.split()[1:]]
            except ValueError:
                return None
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
            return sum(values[:8]) - idle, sum(values[:8])
    return None


def cpu_percent(before: tuple[int, int] | None, after: tuple[int, int] | None) -> float | None:
    if before is None or after is None:
        return None
    total = after[1] - before[1]
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (after[0] - before[0]) / total)), 1)


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """(전체 MB, 쓰는 중 MB). MemAvailable 기준."""
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


class NodeMetrics:
    """`sample()`이 보고에 넣을 필드 dict를 돌려준다. 값이 하나라도 없으면 None(보고에서 뺀다)."""

    def __init__(self, proc: str | Path = "/proc"):
        self.proc = Path(proc)
        self._cpu: tuple[int, int] | None = None

    def _read(self, name: str) -> str | None:
        try:
            return (self.proc / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def sample(self) -> dict | None:
        stat, meminfo = self._read("stat"), self._read("meminfo")
        now = parse_cpu(stat) if stat else None
        cpu, self._cpu = cpu_percent(self._cpu, now), now
        memory = parse_meminfo(meminfo) if meminfo else None
        if cpu is None or memory is None:
            return None
        total_mb, used_mb = memory
        return {"cpu_percent": cpu, "mem_used_mb": used_mb, "mem_total_mb": total_mb}
