"""Ticket 32: the periodic sweep of expired Sandbox Namespaces."""

import pytest
from cmp_worker.workflows import sandbox_expire


@pytest.mark.asyncio
async def test_schedule_is_created_once_and_skips_an_overlapping_run():
    from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy

    class Client:
        def __init__(self):
            self.created = []

        async def create_schedule(self, schedule_id, schedule):
            if self.created:
                raise ScheduleAlreadyRunningError()
            self.created.append((schedule_id, schedule))

    client = Client()
    assert await sandbox_expire.ensure_sandbox_expire_schedule(client) is True
    assert await sandbox_expire.ensure_sandbox_expire_schedule(client) is False
    [(schedule_id, schedule)] = client.created
    assert schedule_id == "cmp-sandbox-expire-sweep"
    assert schedule.spec.intervals[0].every.total_seconds() == 15 * 60
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP


@pytest.mark.asyncio
async def test_a_schedule_failure_does_not_stop_start_up():
    class Client:
        async def create_schedule(self, schedule_id, schedule):
            raise RuntimeError("temporal unavailable")

    assert await sandbox_expire.ensure_sandbox_expire_schedule(Client()) is False


def test_the_sweep_activity_is_registered_with_the_sandbox_loop():
    from cmp_worker.activities.sandbox_loop import SandboxLoopActivities

    names = {
        f.__temporal_activity_definition.name
        for f in SandboxLoopActivities.all(object.__new__(SandboxLoopActivities))
    }
    assert {"sbx_teardown", sandbox_expire.SWEEP_ACTIVITY} <= names
