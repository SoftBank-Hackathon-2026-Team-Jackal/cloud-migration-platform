"""How downstream Workflows read the Provisioning handoff (ticket 50, #185).

Two Activities on the Provisioning queue read the stored `ProvisioningResult` by Plan Revision:

- `get_migration_handoff({plan_id, revision, consumer})` returns the consumer view of ticket 13 R7
  (`{plan_id, revision, consumer, common, database|application, proceed, blocked_by}`). Data
  Migration (`cmp_data.handoff.target_endpoint`) reads the DATA_MIGRATION view.
- `get_traffic_handover({plan_id, revision})` returns `TrafficHandoff`: the TRAFFIC_MIGRATION view
  already converted into the Traffic `Handover` shape (`cmp_traffic.contracts.Handover`), so the
  parent Workflow passes `handover` to `TrafficShiftWorkflow` unchanged (#128).

The view is read at call time: ON_PREM tunnel evidence is merged when read (tickets 16/25), so a
later call can proceed where an earlier one was blocked. Nothing here holds a secret value.
"""

from typing import Literal

from pydantic import Field

from cmp_contracts.provisioning import Contract, Positive, Text

MIGRATION_HANDOFF_ACTIVITY = "get_migration_handoff"
TRAFFIC_HANDOVER_ACTIVITY = "get_traffic_handover"
# `MigrationWorkflow` reads the traffic handover after a successful execution under this patch.
TRAFFIC_HANDOFF_PATCH = "traffic-handoff-v1"
# `MigrationWorkflow` runs the real `TrafficShiftWorkflow` child (#128) under this patch when the
# start input carries `traffic`; without it the scaffold `shift_traffic` step stays.
TRAFFIC_SHIFT_PATCH = "traffic-shift-v1"
# Ticket 67 (#284): an ON_PREM lookup waits inside the Activity for this rollout's tunnel health
# check, so under this patch the Workflow schedules it with a longer timeout and a heartbeat.
TRAFFIC_HANDOVER_WAIT_PATCH = "traffic-handover-wait-v1"
TRAFFIC_HANDOVER_START_TO_CLOSE_S = 600
TRAFFIC_HANDOVER_HEARTBEAT_S = 30
# The configured wait is cut to this, leaving a minute of the timeout for the reads themselves.
TRAFFIC_HANDOVER_WAIT_MAX_S = TRAFFIC_HANDOVER_START_TO_CLOSE_S - 60
# Ticket 80 (#319): the ON_PREM DATA_MIGRATION lookup waits the same way for the tunnel DB check;
# under this patch the Workflow schedules it with the ticket 67 timeout and heartbeat.
DATA_HANDOFF_WAIT_PATCH = "data-handoff-wait-v1"

# Why a view does not proceed (ticket 13 R7, 16 T2/T7/T11, 25 F3). The first code is the cause.
BlockCode = Literal[
    "RESULT_NOT_FOUND",  # no stored result for the Revision (not executed, or another Revision)
    "WORKLOADS_NOT_DEPLOYED",  # INFRASTRUCTURE_READY: infra only
    "PROVISIONING_NOT_READY",  # FAILED / UNKNOWN
    "DATABASE_NOT_READY",  # DATA_MIGRATION
    "TUNNEL_DB_NOT_VERIFIED",  # DATA_MIGRATION, ON_PREM: no passed DB check over the tunnel
    "APPLICATION_NOT_READY",  # TRAFFIC_MIGRATION
    "EXTERNAL_ENDPOINT_UNAVAILABLE",  # TRAFFIC_MIGRATION: no forward destination (yet)
    "TUNNEL_STATUS_STALE",  # ON_PREM: the tunnel report is too old to count as evidence
]
BLOCK_CODES: tuple[str, ...] = BlockCode.__args__
# The lookup itself failed (Activity error after retries): the Workflow cannot tell whether the
# target is ready, so it treats this like a blocked view and does not start the shift.
HANDOFF_UNAVAILABLE = "HANDOFF_UNAVAILABLE"


class HandoffRequest(Contract):
    plan_id: Text
    revision: Positive
    consumer: Literal["DATA_MIGRATION", "TRAFFIC_MIGRATION"]


class TrafficHandoverRequest(Contract):
    plan_id: Text
    revision: Positive


class TrafficHandover(Contract):
    """Field-for-field the Traffic `Handover` (owned by Traffic); a contract test pins the match.

    `port` is the port the relay forwards to (AWS/GCP: the LB port, 80; ON_PREM: the app port on
    the tunnel side). It is never the in-cluster container port of an AWS/GCP app.
    """

    status: Literal["READY", "FAILED", "UNKNOWN"]
    ready: bool
    external_endpoint: Text | None = None
    health_path: Text = "/health"
    port: int = Field(default=80, ge=1, le=65535)


class TrafficHandoff(Contract):
    contract_version: Literal["1"] = "1"
    plan_id: Text
    revision: Positive
    provider: Literal["AWS", "GCP", "ON_PREM"] | None = None  # None when no result is stored
    proceed: bool
    blocked_by: list[Text] = Field(default_factory=list)
    # Always present; `ready` is False whenever `proceed` is False, so a consumer that ignores
    # `proceed` still cannot pass `check_handover`.
    handover: TrafficHandover
