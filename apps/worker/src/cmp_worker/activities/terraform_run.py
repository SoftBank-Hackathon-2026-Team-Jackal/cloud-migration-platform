"""Run a blocking Terraform call in a thread with heartbeats and graceful cancellation (53 D9).

While the thread runs the Activity heartbeats every `heartbeat_seconds`. When Temporal cancels
the Activity (Workflow cancel, heartbeat timeout, worker shutdown), the run's stop event is set:
the current Terraform command gets SIGTERM → grace → SIGKILL (`terraform_process.run_graceful`),
and the Activity still waits for the thread so the interrupted result (lock and State checks) is
recorded before the cancellation propagates.
"""

import asyncio
import contextvars
import threading
from collections.abc import Callable

from cmp_providers.terraform_process import stop_requested
from temporalio import activity

HEARTBEAT_SECONDS = 30


async def run_with_heartbeat(fn: Callable[..., dict], *args, heartbeat_seconds: float = 30):
    stop = threading.Event()
    with stop_requested(stop):
        context = contextvars.copy_context()  # carries the stop event into the thread
    activity.heartbeat("started")
    work = asyncio.ensure_future(asyncio.to_thread(context.run, fn, *args))
    try:
        while not work.done():
            await asyncio.wait({work}, timeout=heartbeat_seconds)
            if not work.done():
                activity.heartbeat("running")
    except asyncio.CancelledError:
        stop.set()
        result = await asyncio.shield(work)
        activity.logger.warning(
            "terraform run cancelled", extra={"code": (result or {}).get("code")}
        )
        raise
    return work.result()
