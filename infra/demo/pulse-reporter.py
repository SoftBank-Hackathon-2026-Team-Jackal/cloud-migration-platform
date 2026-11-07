"""서버의 CPU·메모리 사용률을 플랫폼 `POST /system/pulse/report`로 3초마다 보낸다(웹 상단 위젯의 원본·이전 서버 값).

표준 라이브러리만 쓴다. Linux는 /proc, macOS는 top·vm_stat·sysctl로 읽는다. 읽은 값(퍼센트와 MB)만 보내고
그 밖의 정보(프로세스, 주소, 파일)는 읽지도 보내지도 않는다. 토큰은 로그에 쓰지 않는다.

환경변수:
  PULSE_URL     보고 주소. 예: https://<플랫폼 호스트>/system/pulse/report
  PULSE_TOKEN   플랫폼 `PULSE_REPORT_TOKEN`과 같은 값(헤더 X-Pulse-Token)
  PULSE_ROLE    source(원본 서버) | target(이전 서버)
  PULSE_NODE    이 서버를 알아볼 이름(영문·숫자·._-, 40자 이하). 기본은 호스트 이름
  PULSE_INTERVAL  보내는 간격(초, 기본 3)

실행: python3 pulse-reporter.py            # 계속 보낸다
      python3 pulse-reporter.py --dry-run  # 한 번 읽어서 보낼 내용만 출력(보내지 않는다)
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def parse_proc_cpu(stat: str) -> tuple[int, int] | None:
    """`/proc/stat` 첫 줄 → (바쁜 시간, 전체 시간)."""
    for line in stat.splitlines():
        if line.startswith("cpu "):
            try:
                values = [int(part) for part in line.split()[1:]]
            except ValueError:
                return None
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values[:8])
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
    """(전체 MB, 쓰는 중 MB). MemAvailable 기준."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if key in ("MemTotal", "MemAvailable") and parts and parts[0].isdigit():
            values[key] = int(parts[0])
    if "MemTotal" not in values or "MemAvailable" not in values or values["MemTotal"] <= 0:
        return None
    total = values["MemTotal"]
    return total // 1024, (total - values["MemAvailable"]) // 1024


def parse_top_cpu(output: str) -> float | None:
    """macOS `top -l 2 -n 0`의 마지막 `CPU usage: ... 91.6% idle` 줄 → 사용률."""
    idle = None
    for line in output.splitlines():
        match = re.search(r"CPU usage:.*?([0-9.]+)% idle", line)
        if match:
            idle = float(match.group(1))
    return None if idle is None else round(max(0.0, min(100.0, 100.0 - idle)), 1)


def parse_vm_stat(output: str, total_bytes: int) -> tuple[int, int] | None:
    """macOS `vm_stat` → (전체 MB, 쓰는 중 MB). free·inactive·speculative 페이지는 쓸 수 있는 메모리로 본다."""
    size = re.search(r"page size of (\d+) bytes", output)
    if not size or total_bytes <= 0:
        return None
    page = int(size.group(1))
    pages = {}
    for line in output.splitlines():
        name, _, value = line.partition(":")
        digits = value.strip().rstrip(".")
        if digits.isdigit():
            pages[name.strip()] = int(digits)
    available = (
        sum(pages.get(key, 0) for key in ("Pages free", "Pages inactive", "Pages speculative"))
        * page
    )
    used = max(0, total_bytes - available)
    return total_bytes // (1024 * 1024), used // (1024 * 1024)


def run(*command: str) -> str:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def sample_linux(previous: tuple[int, int] | None) -> tuple[dict | None, tuple[int, int] | None]:
    """한 번 읽는다. 이전 읽기와의 차이로 CPU를 구하므로 첫 호출은 CPU가 없어 None을 돌려준다."""
    now = parse_proc_cpu(read("/proc/stat"))
    memory = parse_meminfo(read("/proc/meminfo"))
    cpu = cpu_percent(previous, now)
    if cpu is None or memory is None:
        return None, now
    return {"cpu_percent": cpu, "mem_total_mb": memory[0], "mem_used_mb": memory[1]}, now


def sample_macos() -> dict | None:
    cpu = parse_top_cpu(run("top", "-l", "2", "-n", "0", "-s", "1"))
    total = run("sysctl", "-n", "hw.memsize").strip()
    memory = parse_vm_stat(run("vm_stat"), int(total)) if total.isdigit() else None
    if cpu is None or memory is None:
        return None
    return {"cpu_percent": cpu, "mem_total_mb": memory[0], "mem_used_mb": memory[1]}


def node_name() -> str:
    raw = os.environ.get("PULSE_NODE") or socket.gethostname()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw)[:40]
    return cleaned or "node"


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
    url = os.environ.get("PULSE_URL", "")
    token = os.environ.get("PULSE_TOKEN", "")
    role = os.environ.get("PULSE_ROLE", "")
    interval = max(1.0, float(os.environ.get("PULSE_INTERVAL", "3")))
    if role not in ("source", "target"):
        print("PULSE_ROLE은 source 또는 target이어야 합니다.", file=sys.stderr)
        return 2
    if not dry and not (url and token):
        print("PULSE_URL과 PULSE_TOKEN이 필요합니다.", file=sys.stderr)
        return 2
    node = node_name()
    mac = platform.system() == "Darwin"
    previous = parse_proc_cpu(read("/proc/stat")) if not mac else None
    last_status = None
    while True:
        if mac:
            values = sample_macos()
        else:
            time.sleep(1 if dry else interval)
            values, previous = sample_linux(previous)
        if values:
            body = {"role": role, "node": node, **values}
            if dry:
                print(json.dumps(body, ensure_ascii=False))
                return 0
            status = post(url, token, body)
            if status != last_status:  # 상태가 바뀔 때만 한 줄 남긴다(토큰·값은 쓰지 않는다)
                print(f"report status {status}", flush=True)
                last_status = status
        elif dry:
            print("값을 읽지 못했습니다.", file=sys.stderr)
            return 1
        if mac:
            time.sleep(max(0.0, interval - 1.0))  # top이 이미 1초 걸린다


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
