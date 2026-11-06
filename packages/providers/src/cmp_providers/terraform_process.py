"""Run Terraform (and its helper CLIs) so a timeout or cancellation never SIGKILLs first (53).

`run_graceful` sends SIGTERM at the deadline (or when the run's stop event is set), waits
`GRACE_SECONDS` for Terraform's "Gracefully shutting down" path — which releases the State lock
and writes what it created — and only then sends SIGKILL (D8). Measured with Terraform 1.10.5 and
1.17.0: SIGTERM releases the lock and records the in-flight resource; SIGKILL leaves the lock and
loses it.

After an interrupted or failed Apply, `s3_lock_state` reports whether the S3 `.tflock` object is
still there and `preserve_errored_state` keeps a `errored.tfstate` Terraform wrote when it could
not save the remote State (D10). Command output is never returned to callers' results.
"""

import contextlib
import contextvars
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

GRACE_SECONDS = int(os.environ.get("TERRAFORM_GRACE_SECONDS", "300"))
_POLL_SECONDS = 0.2
# Set by an Activity before it hands the run to a worker thread (`asyncio.to_thread` copies the
# context); setting the event stops the current command gracefully.
_STOP: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "terraform_stop", default=None
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    # None: exited by itself. TERMINATED: ended within the grace period after SIGTERM.
    # KILLED: needed SIGKILL. Either stop sets `returncode` to -1 (interrupted).
    stopped: str | None = None


@contextlib.contextmanager
def stop_requested(event: threading.Event) -> Iterator[threading.Event]:
    token = _STOP.set(event)
    try:
        yield event
    finally:
        _STOP.reset(token)


def run_graceful(
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    *,
    sink: Callable[[bytes], None] | None = None,
    stop: threading.Event | None = None,
    grace_seconds: float | None = None,
) -> CommandResult:
    stop = stop if stop is not None else _STOP.get()
    grace = GRACE_SECONDS if grace_seconds is None else grace_seconds
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return CommandResult(-1, b"", b"")
    lines: list[bytes] = []
    errors: list[bytes] = []

    def read_stdout() -> None:
        for line in proc.stdout:
            lines.append(line)
            if sink is not None:
                sink(line)

    readers = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    stopped = None
    while proc.poll() is None:
        if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
                stopped = "TERMINATED"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                stopped = "KILLED"
            break
        try:
            proc.wait(timeout=_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            continue
    for reader in readers:
        reader.join(timeout=5)
    code = -1 if stopped else proc.returncode
    return CommandResult(code, b"".join(lines), b"".join(errors), stopped)


def preserve_errored_state(work: Path, keep_dir: Path, label: str) -> str | None:
    """Move `errored.tfstate` out of a workspace that is about to be deleted; its file name."""
    source = work / "errored.tfstate"
    if not source.is_file():
        return None
    keep_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{label}-{int(time.time())}.tfstate"
    target = keep_dir / name
    shutil.move(source, target)
    os.chmod(target, 0o600)
    return name


def s3_lock_state(
    runner, *, bucket: str, key: str, region: str, profile: str, cwd: Path, env: dict[str, str]
) -> str:
    """HELD / ABSENT / UNKNOWN for the S3-native lock object (`use_lockfile=true`)."""
    result = runner(
        ["aws", "s3api", "head-object", "--bucket", bucket, "--key", f"{key}.tflock",
         "--region", region, "--profile", profile, "--output", "json"],
        cwd,
        env,
        60,
    )  # fmt: skip
    if result.returncode == 0:
        return "HELD"
    text = result.stderr.lower()
    return "ABSENT" if b"404" in text or b"not found" in text else "UNKNOWN"


def failed_apply_refs(
    result: CommandResult,
    *,
    runner,
    work: Path,
    keep_dir: Path,
    label: str,
    lock: dict[str, str],
    env: dict[str, str],
) -> dict[str, str]:
    """D10 refs after a non-zero Apply/destroy: kept `errored.tfstate`, stop kind, lock state.

    `lock` holds `bucket`, `key`, `region` and the State (platform) `profile`."""
    refs = {}
    if preserve_errored_state(work, keep_dir, label):
        refs["state_recovery"] = "PRESERVED"
    if result.returncode < 0:
        if result.stopped:
            refs["stopped"] = result.stopped
        refs["state_lock"] = s3_lock_state(runner, cwd=work, env=env, **lock)
    return refs
