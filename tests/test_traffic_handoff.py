"""Ticket 50 (#185): how the parent Workflow reads the handoff, and the Traffic `Handover` shape.

Seams:
- Contract: real `build_provisioning_result` → real `handoff` view (ticket 13/16/25) → Provisioning
  converter → the real Traffic `cmp_traffic.contracts.Handover` and `check_handover`. Provider refs
  are the recorded real result shapes of tickets 09/11/12 (reused from ticket 13 tests).
- Activity: `ApprovalActivities.get_traffic_handover` on SQLite with a stored result.
- Workflow: Temporal time-skipping server, real `MigrationWorkflow`/approval Workflow, fake
  Provisioning Activities; every run is replayed, plus a history recorded before this ticket.
Misses: a real relay/Envoy, a real TrafficShiftWorkflow child (#128), a real cluster.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cmp_api.event_stream import migration_status
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.handoff import (
    BLOCK_CODES,
    HANDOFF_UNAVAILABLE,
    TRAFFIC_HANDOVER_ACTIVITY,
    TrafficHandoff,
    TrafficHandover,
)
from cmp_providers.provisioning_result import build_provisioning_result, handoff, traffic_handover
from cmp_traffic import check_handover
from cmp_traffic.contracts import Handover
from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.events import configure_emitter
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer
from test_migration_plan_workflow import PLAN, FakeProvisioning, deliver, run_scenario, wait_phase
from test_provisioning_result import (  # noqa: F401  (`db` is a fixture)
    AWS_REFS,
    GCP_WORKLOAD_REFS,
    ONPREM_REFS,
    Approved,
    complete_gcp,
    db,
    plan_manifest,
)

PRE_50_HISTORY = Path(__file__).parent / "fixtures/temporal/migration-onprem-executed-pre50.json"
FINISHED = datetime(2026, 11, 6, tzinfo=UTC)
TUNNEL_OK = {"tunnel_ip": "10.200.0.2", "app_health_ok": True, "db_connect_ok": True}


def view(provider, scope="WORKLOADS", *, status="SUCCEEDED", refs=None, tunnel=None, step=None):
    default = {"AWS": AWS_REFS, "GCP": GCP_WORKLOAD_REFS, "ON_PREM": ONPREM_REFS}[provider]
    result = build_provisioning_result(
        plan_manifest(provider, scope),
        approval_id="approval-1",
        status=status,
        code="WORKLOADS_READY" if status == "SUCCEEDED" else "APPLY_FAILED",
        step=step,
        refs=default if refs is None else refs,
        finished_at=FINISHED,
    )
    return handoff(result, "TRAFFIC_MIGRATION", "plan-001", 1, tunnel)


def as_traffic(converted: TrafficHandoff) -> Handover:
    """The real Traffic model must accept the converted value as it is."""
    return Handover.model_validate(converted.handover.model_dump())


# ---- contract: the converted value is a valid Traffic Handover -------------------------------


def test_the_handover_model_mirrors_the_traffic_model_field_for_field():
    assert set(TrafficHandover.model_fields) == set(Handover.model_fields)
    for name, field in Handover.model_fields.items():
        assert TrafficHandover.model_fields[name].default == field.default, name


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    [
        ("AWS", "http://k8s-cmpdemot-cmpdemoa-1.ap-northeast-2.elb.amazonaws.com:80"),
        ("GCP", "http://34.102.136.6:80"),
    ],
)
def test_cloud_ready_view_forwards_to_the_lb_port_not_the_app_port(provider, endpoint):
    converted = traffic_handover(view(provider))

    assert converted.proceed and converted.blocked_by == []
    assert converted.provider == provider
    handover = as_traffic(converted)
    assert (handover.status, handover.ready) == ("READY", True)
    assert handover.external_endpoint == endpoint
    assert handover.port == 80  # the LB port; the app listens on 8080 inside the cluster
    assert handover.health_path == "/health"
    assert check_handover(handover).ok


def test_onprem_view_forwards_to_the_tunnel_side_app_once_verified():
    converted = traffic_handover(view("ON_PREM", "FULL", tunnel=TUNNEL_OK))

    handover = as_traffic(converted)
    assert converted.proceed
    assert (handover.external_endpoint, handover.port) == ("http://10.200.0.2:8080", 8080)
    assert check_handover(handover).ok


@pytest.mark.parametrize(
    ("kwargs", "code", "status"),
    [
        ({"provider": "ON_PREM", "scope": "FULL"}, "EXTERNAL_ENDPOINT_UNAVAILABLE", "READY"),
        (
            {"provider": "ON_PREM", "scope": "FULL", "tunnel": TUNNEL_OK | {"stale": True}},
            "TUNNEL_STATUS_STALE",
            "READY",
        ),
        (
            {"provider": "GCP", "scope": "INFRASTRUCTURE", "refs": {}},
            "WORKLOADS_NOT_DEPLOYED",
            "UNKNOWN",
        ),
        (
            {"provider": "GCP", "status": "FAILED", "step": "post_deploy_verification"},
            "PROVISIONING_NOT_READY",
            "FAILED",
        ),
    ],
)
def test_a_blocked_view_never_passes_the_traffic_precheck(kwargs, code, status):
    provider = kwargs.pop("provider")
    converted = traffic_handover(view(provider, **kwargs))

    assert not converted.proceed and converted.blocked_by[0] == code
    assert code in BLOCK_CODES
    handover = as_traffic(converted)  # INFRASTRUCTURE_READY is representable (as UNKNOWN)
    assert handover.status == status and handover.ready is False
    assert not check_handover(handover).ok


def test_a_missing_result_is_an_unknown_handover():
    converted = traffic_handover(handoff(None, "TRAFFIC_MIGRATION", "plan-001", 1))

    assert converted.blocked_by == ["RESULT_NOT_FOUND"] and converted.provider is None
    assert as_traffic(converted).status == "UNKNOWN"


def test_the_data_view_still_feeds_the_data_migration_input():
    """Fields Data Migration reads (#185 comment): kept as they are by this ticket."""
    from cmp_data.handoff import DataMigrationInput, target_endpoint

    result = build_provisioning_result(
        plan_manifest("GCP", "WORKLOADS"),
        approval_id="approval-1",
        status="SUCCEEDED",
        code="WORKLOADS_READY",
        step=None,
        refs=GCP_WORKLOAD_REFS,
        finished_at=FINISHED,
    )
    data = handoff(result, "DATA_MIGRATION", "plan-001", 1)
    given = DataMigrationInput.model_validate(
        {
            "source": {
                "access": "KUBECONFIG",
                "kubeconfig_ref": "src",
                "namespace": "s",
                "pod": "s-0",
                "peer_host": "10.0.0.1",
                "dbname": "demoapp",
            },
            "app": {},
            "settings": {},
            "target_peer_host": "10.200.0.9",
        }
    )
    target, code = target_endpoint(data, given)
    assert code is None
    assert (target.access, target.pod, target.namespace) == (
        "GCP_GKE", "cmp-demo-db-0", "cmp-demo-target")  # fmt: skip
    assert (target.cluster_name, target.project_id) == ("cmp-target-gke", "sbh26-prod")


# ---- Activity -------------------------------------------------------------------------------


async def test_activity_reads_the_stored_result_and_returns_the_converted_handoff(db):  # noqa: F811
    approved = Approved(db, "GCP", "WORKLOADS")
    complete_gcp(db, approved)

    got = await ActivityEnvironment().run(
        ApprovalActivities(db).get_traffic_handover, {"plan_id": "plan-001", "revision": 1}
    )

    parsed = TrafficHandoff.model_validate(got)
    assert parsed.proceed and (parsed.plan_id, parsed.revision) == ("plan-001", 1)
    assert check_handover(Handover.model_validate(got["handover"])).ok
    assert got["handover"]["external_endpoint"] == "http://34.102.136.6:80"


async def test_activity_reports_a_missing_revision_as_blocked(db):  # noqa: F811
    got = await ActivityEnvironment().run(
        ApprovalActivities(db).get_traffic_handover, {"plan_id": "plan-404", "revision": 1}
    )
    assert got["proceed"] is False and got["blocked_by"] == ["RESULT_NOT_FOUND"]


# ---- Workflow: read after a successful execution, keep it for the traffic step ----------------


@pytest.fixture
def emitter():
    stored = InMemoryEmitter()
    previous = configure_emitter(stored)
    yield stored
    configure_emitter(previous)


READY_HANDOFF = traffic_handover(view("GCP")).model_dump(mode="json")


def scheduled_activities(history) -> list[str]:
    started = EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    return [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.event_type == started
    ]


async def run_executed(fake, after=None):
    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await deliver(env)
        outcome = await handle.result()
        if after is not None:  # ticket 64: read the run's history before the env closes
            await after(handle)
        return (
            outcome,
            await handle.query("status"),
            scheduled_activities(await handle.fetch_history()),
        )

    return await run_scenario(fake, scenario)


async def test_successful_execution_reads_the_handover_before_the_traffic_step(emitter):
    fake = FakeProvisioning(traffic=READY_HANDOFF)

    outcome, state, activities = await run_executed(fake)

    assert outcome == "scaffold_complete"
    assert fake.handover_requests == [PLAN]
    # Kept in Workflow state for the traffic step (#128 passes `handover` to the child as is).
    assert state["traffic_handoff"] == READY_HANDOFF
    assert Handover.model_validate(state["traffic_handoff"]["handover"]).ready
    order = [name for name in activities if name in (TRAFFIC_HANDOVER_ACTIVITY, "shift_traffic")]
    assert order == [TRAFFIC_HANDOVER_ACTIVITY, "shift_traffic"]
    assert emitter.of_type("error") == []


async def test_a_blocked_handover_stops_before_the_traffic_step(emitter):
    blocked = traffic_handover(view("ON_PREM", "FULL")).model_dump(mode="json")
    fake = FakeProvisioning(traffic=blocked)

    outcome, state, activities = await run_executed(fake)

    assert outcome == "traffic_blocked"
    assert state["phase"] == "traffic_blocked"
    assert state["failure_code"] == "EXTERNAL_ENDPOINT_UNAVAILABLE"
    assert state["failure_phase"] == "traffic"
    assert state["failure_issues"] == ["EXTERNAL_ENDPOINT_UNAVAILABLE"]
    assert state["traffic_handoff"] == blocked
    assert "shift_traffic" not in activities and "verify_target" not in activities
    [error] = emitter.of_type("error")
    assert error.data["code"] == "EXTERNAL_ENDPOINT_UNAVAILABLE"
    assert (error.phase, error.status) == ("traffic", "failed")
    assert migration_status(state, None) == "failed"  # the SSE closes as failed


async def test_a_failed_lookup_is_blocked_not_a_workflow_failure(emitter):
    fake = FakeProvisioning(traffic=ApplicationError("db down", type="Boom", non_retryable=True))

    outcome, state, activities = await run_executed(fake)

    assert outcome == "traffic_blocked"
    assert state["failure_code"] == HANDOFF_UNAVAILABLE
    assert state["traffic_handoff"] is None
    assert "shift_traffic" not in activities


async def test_history_recorded_before_ticket_50_replays_without_the_lookup():
    history = WorkflowHistory.from_json("migration-1", PRE_50_HISTORY.read_text())
    assert TRAFFIC_HANDOVER_ACTIVITY not in scheduled_activities(history)
    await Replayer(
        workflows=[
            MigrationWorkflow,
            AnalysisWorkflow,
            SandboxVerificationWorkflow,
            ProvisioningApprovalWorkflow,
        ]
    ).replay_workflow(history)


def test_phase_changes_keep_the_data_migration_state_for_the_db_gate():
    """The traffic step changes the phase; the data child's state (#128 `db_gate_ok`) survives."""
    flow = MigrationWorkflow()
    flow.data_migration = {"phase": "switched", "writer": "target", "reverse_replication_ok": True}
    flow.phase = "traffic"
    assert flow.data_migration["writer"] == "target"
    assert flow.db_gate_ok() is True
