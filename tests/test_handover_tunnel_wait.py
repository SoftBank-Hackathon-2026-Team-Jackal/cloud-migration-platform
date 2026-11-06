"""Ticket 67 (#284): the ON_PREM traffic handover waits for a tunnel health check of this rollout.

Seams:
- Activity: `ApprovalActivities.get_traffic_handover` on SQLite with a stored result and an
  `agent_tunnel_status` row, a fake clock/sleep (the Agent's next report is written when the fake
  time passes it) and `ActivityEnvironment` (heartbeats). Events go to an `InMemoryEmitter`.
- Pure: `tunnel_wait_needed` (which blocks clear by themselves) and `after_rollout` (a report from
  before the rollout is no evidence).
- Workflow: Temporal time-skipping server, real `MigrationWorkflow`, the real Activity (fake clock)
  behind the Provisioning fake; the run is replayed and the scheduled timeouts are read back.
Misses: a real Agent tunnel container (ticket 65), a real relay.
"""

from datetime import UTC, datetime

import pytest
from cmp_api.approvals import claim_execution, complete_execution
from cmp_api.migration_events import InMemoryEmitter
from cmp_api.tunnels import TunnelStatus
from cmp_contracts.handoff import (
    TRAFFIC_HANDOVER_ACTIVITY,
    TRAFFIC_HANDOVER_HEARTBEAT_S,
    TRAFFIC_HANDOVER_WAIT_MAX_S,
    TrafficHandoff,
)
from cmp_providers.provisioning_result import after_rollout, tunnel_wait_needed
from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.events import configure_emitter
from temporalio.api.enums.v1 import EventType
from temporalio.testing import ActivityEnvironment
from test_migration_plan_workflow import FakeProvisioning
from test_provisioning_result import (  # noqa: F401  (`db` is a fixture)
    ONPREM_REFS,
    Approved,
    complete_gcp,
    db,
)
from test_traffic_handoff import run_executed

T0 = 1_794_000_000  # rollout result recorded (epoch seconds)
REQUEST = {"plan_id": "plan-001", "revision": 1}
AGENT = "agent-001"


class FakeTime:
    """One clock for tunnel freshness (epoch), the wait deadline and sleeps."""

    def __init__(self, start: float):
        self.t = float(start)
        self.sleeps: list[float] = []
        self.reports: list[tuple[float, dict]] = []  # (at, status) written once time passes `at`
        self.db = None

    def clock(self) -> int:
        return int(self.t)

    def timer(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        due = [r for r in self.reports if r[0] <= self.t]
        self.reports = [r for r in self.reports if r[0] > self.t]
        for at, status in due:
            report(self.db, reported_at=int(at), **status)


def report(factory, *, reported_at: int, app_health_ok: bool, db_connect_ok: bool = True):
    with factory() as session:
        row = session.get(TunnelStatus, AGENT) or TunnelStatus(agent_id=AGENT)
        row.tunnel_ip, row.last_handshake_at = "10.200.0.2", reported_at
        row.app_health_ok, row.db_connect_ok, row.reported_at = (
            app_health_ok,
            db_connect_ok,
            reported_at,
        )
        row.code = None if app_health_ok else "APP_PORT_NOT_ASSIGNED"
        session.add(row)
        session.commit()


def complete_onprem(factory):
    approved = Approved(factory, "ON_PREM", "FULL")
    with factory() as session:
        claim_execution(session, approved.approval_id)
        complete_execution(
            session,
            approved.approval_id,
            status="SUCCEEDED",
            code="READY",
            step="execute_on_prem_plan",
            resource_refs=ONPREM_REFS,
            now=datetime.fromtimestamp(T0, UTC),
        )
    return approved


@pytest.fixture
def emitter():
    stored = InMemoryEmitter()
    previous = configure_emitter(stored)
    yield stored
    configure_emitter(previous)


def activities(factory, fake: FakeTime, wait_seconds=180, poll_seconds=2):
    fake.db = factory
    return ApprovalActivities(
        factory,
        clock=fake.clock,
        timer=fake.timer,
        sleep=fake.sleep,
        wait_seconds=wait_seconds,
        poll_seconds=poll_seconds,
    )


async def run(acts: ApprovalActivities, beats: list | None = None) -> TrafficHandoff:
    env = ActivityEnvironment()
    if beats is not None:
        env.on_heartbeat = lambda *details: beats.append(details)
    return TrafficHandoff.model_validate(await env.run(acts.get_traffic_handover, REQUEST))


def checks(emitter) -> list[tuple[str, str, str]]:
    return [(e.data["name"], e.data["state"], e.status) for e in emitter.of_type("provision.check")]


# ---- Activity: wait until the tunnel reports this rollout's app healthy ----------------------


async def test_proceeds_the_moment_the_post_rollout_health_check_passes(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)  # the lookup runs right after the rollout result is recorded
    report(db, reported_at=T0 + 1, app_health_ok=False)  # APP_PORT_NOT_ASSIGNED, as on 11-07
    fake.reports.append((T0 + 66, {"app_health_ok": True}))  # the Agent's next cycle
    beats: list = []

    got = await run(activities(db, fake), beats)

    assert got.proceed and got.blocked_by == []
    assert got.handover.external_endpoint == "http://10.200.0.2:8080"
    assert fake.t - (T0 + 1) == 66  # no later than the first read after the report (2 s poll)
    assert set(fake.sleeps) == {2}
    assert beats  # Temporal liveness while waiting
    assert checks(emitter) == [
        ("tunnel_app_health", "running", "running"),
        ("tunnel_app_health", "done", "running"),
    ]
    [event] = emitter.of_type("provision.check")[:1]
    assert event.phase == "traffic"


async def test_with_the_immediate_tunnel_report_the_handover_goes_on_within_seconds(db, emitter):  # noqa: F811
    """With ticket 66 the Agent reports a healthy app seconds after the port is assigned."""
    complete_onprem(db)
    fake = FakeTime(T0 + 1)
    report(db, reported_at=T0 - 5, app_health_ok=False)  # APP_PORT_NOT_ASSIGNED before rollout
    fake.reports.append((T0 + 3, {"app_health_ok": True}))  # 66: woken by the rollout

    got = await run(activities(db, fake))

    assert got.proceed
    assert fake.t - (T0 + 1) <= 4  # the next 2 s read after the report


async def test_a_healthy_report_from_before_the_rollout_is_not_trusted(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)
    report(db, reported_at=T0 - 10, app_health_ok=True)  # fresh, but older than this rollout
    fake.reports.append((T0 + 20, {"app_health_ok": True}))

    got = await run(activities(db, fake))

    assert got.proceed
    assert fake.t - (T0 + 1) == 20  # waited for the report written after the rollout


async def test_a_pre_rollout_report_alone_ends_blocked_as_stale(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)
    report(db, reported_at=T0 - 10, app_health_ok=True)

    got = await run(activities(db, fake, wait_seconds=30))

    assert not got.proceed and got.blocked_by == ["TUNNEL_STATUS_STALE"]
    assert got.handover.ready is False


async def test_gives_up_at_the_cap_with_the_original_code(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)
    report(db, reported_at=T0 + 1, app_health_ok=False)

    got = await run(activities(db, fake, wait_seconds=180))

    assert not got.proceed and got.blocked_by == ["EXTERNAL_ENDPOINT_UNAVAILABLE"]
    assert fake.t - (T0 + 1) == 180  # the last sleep is cut to the cap
    assert checks(emitter)[-1] == ("tunnel_app_health", "failed", "failed")


async def test_no_report_at_all_waits_then_ends_with_the_original_code(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)

    got = await run(activities(db, fake, wait_seconds=10))

    assert got.blocked_by == ["EXTERNAL_ENDPOINT_UNAVAILABLE"]
    assert fake.t - (T0 + 1) == 10


async def test_an_already_verified_rollout_proceeds_without_waiting(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 5)
    report(db, reported_at=T0 + 3, app_health_ok=True)

    got = await run(activities(db, fake))

    assert got.proceed and fake.sleeps == []
    assert checks(emitter) == []


async def test_cloud_targets_never_wait(db, emitter):  # noqa: F811
    complete_gcp(db, Approved(db, "GCP", "WORKLOADS"))
    fake = FakeTime(T0)

    got = await run(activities(db, fake))

    assert got.proceed and fake.sleeps == []


async def test_a_missing_result_never_waits(db, emitter):  # noqa: F811
    fake = FakeTime(T0)
    acts = activities(db, fake)

    got = TrafficHandoff.model_validate(
        await ActivityEnvironment().run(
            acts.get_traffic_handover, {"plan_id": "plan-404", "revision": 1}
        )
    )

    assert got.blocked_by == ["RESULT_NOT_FOUND"] and fake.sleeps == []


def test_the_configured_wait_never_outlives_the_activity_timeout(db):  # noqa: F811
    acts = ApprovalActivities(db, wait_seconds=10_000)
    assert acts.wait_seconds == TRAFFIC_HANDOVER_WAIT_MAX_S


# ---- pure: which blocks clear by themselves --------------------------------------------------


def handoff_of(provider, blocked, proceed=False):
    return TrafficHandoff.model_validate(
        {
            "plan_id": "p",
            "revision": 1,
            "provider": provider,
            "proceed": proceed,
            "blocked_by": blocked,
            "handover": {
                "status": "READY",
                "ready": proceed,
                "external_endpoint": "http://10.200.0.2:8080" if proceed else None,
                "health_path": "/health",
                "port": 8080,
            },
        }
    )


@pytest.mark.parametrize(
    ("provider", "blocked", "waits"),
    [
        ("ON_PREM", ["EXTERNAL_ENDPOINT_UNAVAILABLE"], True),
        ("ON_PREM", ["TUNNEL_STATUS_STALE"], True),
        ("ON_PREM", ["APPLICATION_NOT_READY"], False),
        ("ON_PREM", ["PROVISIONING_NOT_READY"], False),
        ("ON_PREM", ["RESULT_NOT_FOUND"], False),
        ("ON_PREM", [], False),
        ("GCP", ["EXTERNAL_ENDPOINT_UNAVAILABLE"], False),
        ("AWS", ["EXTERNAL_ENDPOINT_UNAVAILABLE"], False),
    ],
)
def test_only_onprem_tunnel_blocks_are_waited_for(provider, blocked, waits):
    assert tunnel_wait_needed(handoff_of(provider, blocked)) is waits


def test_after_rollout_keeps_a_proceeding_handoff_only_with_a_later_report():
    ready = handoff_of("ON_PREM", [], proceed=True)
    assert after_rollout(ready, {"reported_at": 100}, recorded_at=100) == ready
    for tunnel in ({"reported_at": 99}, {}, None):
        held = after_rollout(ready, tunnel, recorded_at=100)
        assert (held.proceed, held.blocked_by, held.handover.ready) == (
            False, ["TUNNEL_STATUS_STALE"], False)  # fmt: skip
    cloud = handoff_of("GCP", [], proceed=True)
    assert after_rollout(cloud, None, recorded_at=100) == cloud


# ---- Workflow: the real Activity behind the Provisioning queue --------------------------------


def handover_schedule(history):
    scheduled = EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    for event in history.events:
        attrs = event.activity_task_scheduled_event_attributes
        if event.event_type == scheduled and attrs.activity_type.name == TRAFFIC_HANDOVER_ACTIVITY:
            return attrs
    raise AssertionError("no handover lookup scheduled")


async def test_workflow_waits_in_the_activity_and_goes_on_to_the_traffic_step(db, emitter):  # noqa: F811
    complete_onprem(db)
    fake = FakeTime(T0 + 1)
    report(db, reported_at=T0 + 1, app_health_ok=False)
    fake.reports.append((T0 + 30, {"app_health_ok": True}))
    real = activities(db, fake)

    async def lookup(request: dict) -> dict:
        return await real.get_traffic_handover(REQUEST)  # the stored plan is `plan-001`

    provisioning = FakeProvisioning(traffic=lookup)
    captured = {}

    async def scenario_check(handle):
        captured["attrs"] = handover_schedule(await handle.fetch_history())

    outcome, state, scheduled = await run_executed(provisioning, after=scenario_check)

    assert outcome == "scaffold_complete"
    assert state["traffic_handoff"]["proceed"] is True
    assert scheduled.count(TRAFFIC_HANDOVER_ACTIVITY) == 1  # one lookup, the wait is inside it
    attrs = captured["attrs"]
    assert attrs.heartbeat_timeout.ToSeconds() == TRAFFIC_HANDOVER_HEARTBEAT_S
    assert attrs.start_to_close_timeout.ToSeconds() > TRAFFIC_HANDOVER_WAIT_MAX_S
