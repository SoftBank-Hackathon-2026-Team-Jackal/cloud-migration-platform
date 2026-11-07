"""`infra/demo/pulse-reporter.py`: Linux·macOS 값 읽기(파싱)와 보낼 내용. 네트워크와 /proc은 쓰지 않는다."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "pulse_reporter", ROOT / "infra/demo/pulse-reporter.py"
)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)

STAT_A = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 1 1 1\n"
STAT_B = "cpu  200 0 200 750 150 0 0 0 0 0\n"
TOP = """Processes: 400 total
CPU usage: 50.0% user, 10.0% sys, 40.0% idle
Processes: 401 total
CPU usage: 3.2% user, 5.1% sys, 91.7% idle
"""
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               1000.
Pages active:                             5000.
Pages inactive:                           3000.
Pages speculative:                         500.
"""


def test_linux_cpu_is_the_busy_share_between_two_reads() -> None:
    assert (
        reporter.cpu_percent(reporter.parse_proc_cpu(STAT_A), reporter.parse_proc_cpu(STAT_B))
        == 66.7
    )
    assert reporter.parse_proc_cpu("") is None
    assert reporter.cpu_percent(None, reporter.parse_proc_cpu(STAT_B)) is None


def test_linux_memory_counts_available_not_free() -> None:
    mem = "MemTotal: 8192000 kB\nMemFree: 1 kB\nMemAvailable: 2048000 kB\n"
    assert reporter.parse_meminfo(mem) == (8000, 6000)
    assert reporter.parse_meminfo("MemTotal: 1 kB\n") is None


def test_macos_cpu_uses_the_last_top_sample() -> None:
    assert reporter.parse_top_cpu(TOP) == 8.3
    assert reporter.parse_top_cpu("nothing") is None


def test_macos_memory_treats_free_inactive_and_speculative_as_available() -> None:
    total = 16 * 1024 * 1024 * 1024
    available = (1000 + 3000 + 500) * 16384
    expected_used = (total - available) // (1024 * 1024)
    assert reporter.parse_vm_stat(VM_STAT, total) == (16384, expected_used)
    assert reporter.parse_vm_stat("garbage", total) is None
    assert reporter.parse_vm_stat(VM_STAT, 0) is None


def test_the_node_name_is_made_safe_for_the_platform_pattern(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_NODE", "mac book/pro #1" + "x" * 80)
    name = reporter.node_name()
    assert len(name) <= 40 and all(c.isalnum() or c in "._-" for c in name)


def test_dry_run_prints_the_payload_without_sending(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PULSE_ROLE", "source")
    monkeypatch.setenv("PULSE_NODE", "eks-1")
    monkeypatch.setattr(reporter.platform, "system", lambda: "Linux")
    reads = iter([STAT_A, STAT_B])
    monkeypatch.setattr(
        reporter,
        "read",
        lambda path: (
            next(reads)
            if path == "/proc/stat"
            else "MemTotal: 4096000 kB\nMemAvailable: 1024000 kB\n"
        ),
    )
    monkeypatch.setattr(reporter.time, "sleep", lambda _s: None)
    monkeypatch.setattr(reporter, "post", lambda *a, **k: pytest.fail("dry run must not send"))
    assert reporter.main(["--dry-run"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body == {
        "role": "source",
        "node": "eks-1",
        "cpu_percent": 66.7,
        "mem_total_mb": 4000,
        "mem_used_mb": 3000,
    }


def test_the_role_is_required(monkeypatch) -> None:
    monkeypatch.delenv("PULSE_ROLE", raising=False)
    assert reporter.main([]) == 2


def test_url_and_token_are_required_unless_dry_run(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_ROLE", "target")
    monkeypatch.delenv("PULSE_URL", raising=False)
    monkeypatch.delenv("PULSE_TOKEN", raising=False)
    assert reporter.main([]) == 2
