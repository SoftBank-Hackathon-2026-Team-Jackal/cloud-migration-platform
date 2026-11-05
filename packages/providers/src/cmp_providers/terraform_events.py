"""Terraform `apply -json` machine output → per-resource progress (ticket 24, E15).

Only resource address, type, action and state are read from the stream; `@message`, attribute
values and resource IDs are never copied (they may hold sensitive values). Total `m` comes from
`planned_change` messages when Terraform prints them, otherwise from the approved Preview.
"""

import json
import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # aws_apply imports this module
    from cmp_providers.aws_apply import CommandResult, Runner

logger = logging.getLogger(__name__)
LineSink = Callable[[bytes], None]
NETWORK = (
    "vpc", "subnet", "route", "nat_gateway", "internet_gateway", "eip", "security_group",
    "compute_network", "compute_subnetwork", "compute_router", "compute_firewall",
    "compute_address", "compute_global_address", "service_networking",
)  # fmt: skip
CLUSTER = ("eks_cluster", "eks_node_group", "eks_addon", "container_cluster", "container_node_pool")
IDENTITY = ("iam_", "openid_connect", "service_account", "project_iam", "workload_identity")


def resource_key(resource_type: str) -> str:
    """Web `provision.resource.key` category (network/cluster/iam/other)."""
    kind = resource_type.split("_", 1)[1] if "_" in resource_type else resource_type
    if any(kind.startswith(prefix) for prefix in CLUSTER):
        return "cluster"
    if any(kind.startswith(prefix) for prefix in NETWORK):
        return "network"
    if any(prefix in kind for prefix in IDENTITY):
        return "iam"
    return "other"


@dataclass
class ApplyProgress:
    """Feed `terraform apply -json` lines; calls `on_resource(data, progress)` per change."""

    on_resource: Callable[[dict, float], None]
    expected_total: int = 0
    planned: set[str] = field(default_factory=set)
    done: set[str] = field(default_factory=set)
    stale: bool = False

    @property
    def total(self) -> int:
        return max(len(self.planned), self.expected_total, len(self.done), 1)

    def progress(self) -> float:
        return round(100 * len(self.done) / self.total, 1)

    def feed(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "diagnostic":
            summary = str((message.get("diagnostic") or {}).get("summary", "")).lower()
            self.stale = self.stale or "stale" in summary
            return
        hook = message.get("change") if kind == "planned_change" else message.get("hook")
        resource = (hook or {}).get("resource") or {}
        addr, resource_type = resource.get("addr"), resource.get("resource_type")
        if not isinstance(addr, str) or not isinstance(resource_type, str):
            return
        if kind == "planned_change":
            if (hook or {}).get("action") not in ("noop", "read"):
                self.planned.add(addr)
            return
        state = {"apply_start": "running", "apply_complete": "done", "apply_errored": "failed"}
        if kind not in state:
            return
        if kind == "apply_complete":
            self.done.add(addr)
        progress = self.progress()
        data = {
            "id": addr,
            "key": resource_key(resource_type),
            "state": state[kind],
            "progress": progress,
            "name": addr,
        }
        try:
            self.on_resource(data, progress)
        except Exception:  # noqa: BLE001 - progress reporting must never break the Apply
            logger.warning("apply progress callback failed", extra={"type": kind})


def is_stale(result: "CommandResult", progress: ApplyProgress | None = None) -> bool:
    """Saved Plan stale: a JSON diagnostic (with `-json`) or the human stderr text."""
    return (progress is not None and progress.stale) or b"stale" in result.stderr.lower()


def apply_runner(runner: "Runner", progress: ApplyProgress | None) -> "Runner":
    """Runner for `terraform apply -json`: live lines for the real subprocess runner, replayed
    lines for injected (test) runners, the runner itself without progress."""
    from cmp_providers.aws_apply import subprocess_runner

    if progress is None:
        return runner
    if runner is subprocess_runner:
        return streaming_runner(progress.feed)
    return replaying_runner(runner, progress.feed)


def streaming_runner(sink: LineSink) -> "Runner":
    """A subprocess runner that hands each stdout line to `sink` while the command runs."""
    from cmp_providers.aws_apply import CommandResult

    def run(args: list[str], cwd: Path, env: dict[str, str], timeout: int) -> "CommandResult":
        try:
            proc = subprocess.Popen(
                args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError:
            return CommandResult(-1, b"", b"")
        stderr: list[bytes] = []
        reader = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
        reader.start()
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        lines: list[bytes] = []
        try:
            for line in proc.stdout:
                lines.append(line)
                sink(line)
            code = proc.wait()
        finally:
            timer.cancel()
            reader.join(timeout=5)
        timed_out = code < 0
        return CommandResult(-1 if timed_out else code, b"".join(lines), b"".join(stderr))

    return run


def replaying_runner(runner: "Runner", sink: LineSink) -> "Runner":
    """Wrap a batch runner (tests, fakes): feed its stdout lines to `sink` after it returns."""

    def run(args: list[str], cwd: Path, env: dict[str, str], timeout: int) -> "CommandResult":
        result = runner(args, cwd, env, timeout)
        for line in result.stdout.splitlines():
            sink(line)
        return result

    return run
