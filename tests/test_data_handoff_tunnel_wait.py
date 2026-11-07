"""Ticket 80 (#319): the ON_PREM Data handoff waits for a tunnel DB check of this rollout.

Ticket 67 does this for the Traffic handover; on 11-07 the Data lookup ran before the Agent's next
tunnel report and the run ended `data_blocked` with `TUNNEL_DB_NOT_VERIFIED`.

Seams:
- Activity: `ApprovalActivities.get_migration_handoff` (consumer DATA_MIGRATION) on SQLite, with the
  fake clock/sleep of ticket 67 (the Agent's next report is written when the fake time passes it)
  and `ActivityEnvironment` (heartbeats). Events go to an `InMemoryEmitter`.
- Pure: `tunnel_db_wait_needed` and `data_after_rollout`.
- Workflow timeouts: `tests/data/test_data_parent.py`.
Misses: a real Agent tunnel container, a real relay.
"""

from datetime import UTC, datetime

import pytest
from cmp_api.approvals import claim_execution, complete_execution
from cmp_contracts.handoff import TRAFFIC_HANDOVER_WAIT_MAX_S
from cmp_providers.provisioning_result import data_after_rollout, tunnel_db_wait_needed
from temporalio.testing import ActivityEnvironment
from test_handover_tunnel_wait import (  # noqa: F401  (`emitter` is a fixture)
    T0,
    FakeTime,
    activities,
    complete_onprem,
    emitter,
    report,
)
from test_provisioning_result import (  # noqa: F401  (`db` is a fixture)
    ONPREM_REFS,
    Approved,
    complete_gcp,
    db,
)

DATA = {"plan_id": "plan-001", "revision": 1, "consumer": "DATA_MIGRATION"}
DB_OK = {"app_health_ok": True, "db_connect_ok": True}


async def run(acts, request=DATA, beats: list | None = None) -> dict:
    env = ActivityEnvironment()
    if beats is not None:
        env.on_heartbeat = lambda *details: beats.append(details)
    return await env.run(acts.get_migration_handoff, request)


def checks(emitter) -> list[tuple[str, str, str]]:  # noqa: F811
    return [(e.data["name"], e.data["state"], e.status) for e in emitter.of_type("provision.check")]


# ---- Activity: wait until the tunnel reports this rollout's DB reachable ---------------------


async def test_proceeds_once_the_post_rollout_db_check_passes(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)  # the lookup runs right after the rollout result is recorded
    report(db, reported_at=T0 + 1, app_health_ok=False, db_connect_ok=False)
    fake.reports.append((T0 + 15, DB_OK))  # the Agent's next cycle (~15 s), as on 11-07
    beats: list = []

    got = await run(activities(db, fake), beats=beats)

    assert got["proceed"] is True and got["blocked_by"] == []
    assert got["database"]["tunnel_endpoint"] == "10.200.0.2:5432"
    assert fake.t - (T0 + 1) == 14  # the first 2 s read at or after the report (T0 + 15)
    assert set(fake.sleeps) == {2}
    assert beats  # Temporal liveness while waiting
    assert checks(emitter) == [
        ("tunnel_db", "running", "running"),
        ("tunnel_db", "done", "running"),
    ]
    assert {e.phase for e in emitter.of_type("provision.check")} == {"data_copy"}


async def test_gives_up_at_the_cap_with_the_original_code(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)
    report(db, reported_at=T0 + 1, app_health_ok=True, db_connect_ok=False)

    got = await run(activities(db, fake, wait_seconds=180))

    assert got["proceed"] is False and got["blocked_by"] == ["TUNNEL_DB_NOT_VERIFIED"]
    assert got["database"]["tunnel_endpoint"] is None
    assert fake.t - (T0 + 1) == 180
    assert checks(emitter)[-1] == ("tunnel_db", "failed", "failed")


async def test_a_db_report_from_before_the_rollout_is_not_trusted(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)
    report(db, reported_at=T0 - 10, **DB_OK)  # fresh, but older than this rollout

    held = await run(activities(db, fake, wait_seconds=30))

    assert held["blocked_by"] == ["TUNNEL_STATUS_STALE"]
    assert held["database"]["tunnel_endpoint"] is None

    fake.reports.append((fake.t + 7, DB_OK))
    got = await run(activities(db, fake))
    assert got["proceed"] is True


async def test_an_already_verified_rollout_proceeds_without_waiting(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 5)
    report(db, reported_at=T0 + 3, **DB_OK)

    got = await run(activities(db, fake))

    assert got["proceed"] is True and fake.sleeps == []
    assert checks(emitter) == []


async def test_another_block_is_returned_at_once(db, emitter):  # noqa: F811
    approved = Approved(db, "ON_PREM", "FULL")
    with db() as session:
        claim_execution(session, approved.approval_id)
        complete_execution(
            session,
            approved.approval_id,
            status="FAILED",
            code="ROLLOUT_FAILED",
            step="execute_on_prem_plan",
            resource_refs=ONPREM_REFS,
            now=datetime.fromtimestamp(T0, UTC),
        )
    fake = FakeTime(T0 + 1)

    got = await run(activities(db, fake))

    assert got["proceed"] is False and "PROVISIONING_NOT_READY" in got["blocked_by"]
    assert fake.sleeps == [] and checks(emitter) == []


async def test_cloud_targets_and_missing_results_never_wait(db, emitter):  # noqa: F811
    fake = FakeTime(T0)
    acts = activities(db, fake)
    missing = await run(acts)
    assert missing["blocked_by"] == ["RESULT_NOT_FOUND"]

    complete_gcp(db, Approved(db, "GCP", "WORKLOADS"))
    got = await run(acts)

    assert got["proceed"] is True and fake.sleeps == []


async def test_the_traffic_view_of_this_lookup_does_not_wait(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)

    got = await run(activities(db, fake), {**DATA, "consumer": "TRAFFIC_MIGRATION"})

    assert got["proceed"] is False and fake.sleeps == []


async def test_a_retried_lookup_does_not_repeat_the_check_events(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)
    report(db, reported_at=T0 + 1, app_health_ok=True, db_connect_ok=False)
    fake.reports.append((T0 + 9, DB_OK))
    acts = activities(db, fake)

    await run(acts)
    report(db, reported_at=T0 + 8, app_health_ok=True, db_connect_ok=False)  # flapped
    fake.reports.append((fake.t + 4, DB_OK))
    await run(acts)

    assert checks(emitter) == [
        ("tunnel_db", "running", "running"),
        ("tunnel_db", "done", "running"),
    ]


def test_the_wait_shares_the_traffic_handover_cap(db):  # noqa: F811
    from cmp_worker.activities.approval import ApprovalActivities

    assert ApprovalActivities(db, wait_seconds=10_000).wait_seconds == TRAFFIC_HANDOVER_WAIT_MAX_S


# ---- pure ------------------------------------------------------------------------------------


def view_of(provider, blocked, proceed=False):
    return {
        "plan_id": "p",
        "revision": 1,
        "consumer": "DATA_MIGRATION",
        "common": {"provider": provider},
        "database": {"tunnel_endpoint": "10.200.0.2:5432" if proceed else None},
        "proceed": proceed,
        "blocked_by": blocked,
    }


@pytest.mark.parametrize(
    ("provider", "blocked", "waits"),
    [
        ("ON_PREM", ["TUNNEL_DB_NOT_VERIFIED"], True),
        ("ON_PREM", ["TUNNEL_STATUS_STALE"], True),
        ("ON_PREM", ["TUNNEL_DB_NOT_VERIFIED", "DATABASE_NOT_READY"], False),
        ("ON_PREM", ["DATABASE_NOT_READY"], False),
        ("ON_PREM", ["PROVISIONING_NOT_READY"], False),
        ("ON_PREM", [], False),
        ("GCP", ["TUNNEL_DB_NOT_VERIFIED"], False),
        (None, ["RESULT_NOT_FOUND"], False),
    ],
)
def test_only_onprem_tunnel_db_blocks_are_waited_for(provider, blocked, waits):
    assert tunnel_db_wait_needed(view_of(provider, blocked)) is waits


def test_data_after_rollout_keeps_a_proceeding_view_only_with_a_later_report():
    ready = view_of("ON_PREM", [], proceed=True)
    assert data_after_rollout(ready, {"reported_at": 100}, recorded_at=100) == ready
    for tunnel in ({"reported_at": 99}, {}, None):
        held = data_after_rollout(ready, tunnel, recorded_at=100)
        assert (held["proceed"], held["blocked_by"]) == (False, ["TUNNEL_STATUS_STALE"])
        assert held["database"]["tunnel_endpoint"] is None
    assert ready["proceed"] is True  # the input is not changed
    cloud = view_of("GCP", [], proceed=True)
    assert data_after_rollout(cloud, None, recorded_at=100) == cloud
