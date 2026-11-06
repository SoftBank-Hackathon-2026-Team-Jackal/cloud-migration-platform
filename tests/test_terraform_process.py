"""Ticket 53 (D8, D10) seam: Terraform child processes stop with SIGTERM → grace → SIGKILL.

Real child processes (`python -c`) stand in for Terraform; no Terraform or cloud call is made.
"""

import sys
import threading
import time

from cmp_providers import terraform_process
from cmp_providers.aws_apply import CommandResult, subprocess_runner
from cmp_providers.terraform_events import streaming_runner
from cmp_providers.terraform_process import (
    preserve_errored_state,
    run_graceful,
    s3_lock_state,
    stop_requested,
)

# Exits 0 shortly after SIGTERM, like Terraform's "Gracefully shutting down".
GRACEFUL = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *a: (print('graceful', flush=True), sys.exit(3)))\n"
    "print('started', flush=True)\n"
    "time.sleep(30)\n"
)
# Ignores SIGTERM: only SIGKILL ends it.
STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('started', flush=True)\n"
    "time.sleep(30)\n"
)


def python(code):
    return [sys.executable, "-c", code]


def test_finished_command_keeps_its_exit_code(tmp_path):
    result = run_graceful(python("print('ok')"), tmp_path, {}, 10)
    assert (result.returncode, result.stdout.strip(), result.stopped) == (0, b"ok", None)


def test_timeout_sends_sigterm_first_and_waits_for_a_graceful_exit(tmp_path):
    started = time.monotonic()
    result = run_graceful(python(GRACEFUL), tmp_path, {}, 1, grace_seconds=10)
    assert result.returncode == -1
    assert result.stopped == "TERMINATED"
    assert b"graceful" in result.stdout  # the child saw SIGTERM, not SIGKILL
    assert time.monotonic() - started < 8


def test_child_ignoring_sigterm_is_killed_after_the_grace_period(tmp_path):
    started = time.monotonic()
    result = run_graceful(python(STUBBORN), tmp_path, {}, 1, grace_seconds=1)
    assert (result.returncode, result.stopped) == (-1, "KILLED")
    assert time.monotonic() - started < 8


def test_stop_event_stops_the_child_gracefully_before_the_deadline(tmp_path):
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    result = run_graceful(python(GRACEFUL), tmp_path, {}, 60, grace_seconds=10, stop=stop)
    assert result.stopped == "TERMINATED"


def test_stop_request_from_context_reaches_runners_in_worker_threads(tmp_path):
    stop = threading.Event()
    stop.set()
    with stop_requested(stop):
        result = subprocess_runner(python(GRACEFUL), tmp_path, {}, 60)
    assert result.stopped == "TERMINATED"


def test_lines_reach_the_sink_while_the_child_runs(tmp_path):
    seen = []
    result = run_graceful(python("print('a');print('b')"), tmp_path, {}, 10, sink=seen.append)
    assert seen == [b"a\n", b"b\n"]
    assert result.stdout == b"a\nb\n"


def test_missing_binary_is_a_failed_command(tmp_path):
    result = run_graceful(["/nonexistent/terraform"], tmp_path, {}, 10)
    assert (result.returncode, result.stopped) == (-1, None)


def test_subprocess_and_streaming_runners_use_the_graceful_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(terraform_process, "GRACE_SECONDS", 10)
    batch = subprocess_runner(python(GRACEFUL), tmp_path, {}, 1)
    streamed = streaming_runner(lambda line: None)(python(GRACEFUL), tmp_path, {}, 1)
    assert batch.stopped == streamed.stopped == "TERMINATED"
    assert batch.returncode == streamed.returncode == -1


def test_errored_state_is_moved_out_of_the_workspace_with_private_mode(tmp_path):
    work, keep = tmp_path / "work", tmp_path / "keep"
    work.mkdir()
    (work / "errored.tfstate").write_bytes(b'{"version": 4}')
    name = preserve_errored_state(work, keep, "plan-aws-r1")
    assert name is not None and name.startswith("plan-aws-r1-")
    assert not (work / "errored.tfstate").exists()
    assert (keep / name).read_bytes() == b'{"version": 4}'
    assert (keep / name).stat().st_mode & 0o777 == 0o600
    assert preserve_errored_state(work, keep, "plan-aws-r1") is None


class LockRunner:
    def __init__(self, result):
        self.result, self.commands = result, []

    def __call__(self, args, cwd, env, timeout):
        self.commands.append(args)
        return self.result


def test_s3_lock_state_reads_the_tflock_object_with_the_state_profile(tmp_path):
    held = LockRunner(CommandResult(0, b'{"ContentLength": 120}', b""))
    state = s3_lock_state(
        held, bucket="b", key="cmp/env/terraform.tfstate", region="r", profile="platform",
        cwd=tmp_path, env={},
    )  # fmt: skip
    assert state == "HELD"
    assert held.commands[0][:3] == ["aws", "s3api", "head-object"]
    assert "cmp/env/terraform.tfstate.tflock" in held.commands[0]
    assert held.commands[0][held.commands[0].index("--profile") + 1] == "platform"
    absent = LockRunner(CommandResult(254, b"", b"An error occurred (404) when calling: Not Found"))
    assert s3_lock_state(absent, bucket="b", key="k", region="r", profile="p", cwd=tmp_path,
                         env={}) == "ABSENT"  # fmt: skip
    denied = LockRunner(CommandResult(254, b"", b"An error occurred (403) Forbidden"))
    assert s3_lock_state(denied, bucket="b", key="k", region="r", profile="p", cwd=tmp_path,
                         env={}) == "UNKNOWN"  # fmt: skip


async def test_cancelled_activity_stops_terraform_gracefully_and_waits_for_the_result(tmp_path):
    """D9: Temporal cancellation → stop event → SIGTERM; the result is still produced."""
    import asyncio

    from cmp_worker.activities.terraform_run import run_with_heartbeat
    from temporalio.testing import ActivityEnvironment

    seen = {}

    def apply():
        result = subprocess_runner(python(GRACEFUL), tmp_path, {}, 60)
        seen["stopped"] = result.stopped
        return {"status": "UNKNOWN", "code": "TERRAFORM_APPLY_INTERRUPTED"}

    env = ActivityEnvironment()

    async def body():
        task = asyncio.ensure_future(run_with_heartbeat(apply, heartbeat_seconds=0.1))
        await asyncio.sleep(1)
        env.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "finished"

    assert await env.run(body) == "cancelled"
    assert seen["stopped"] == "TERMINATED"
