from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities import data_cutover, data_migration, traffic
from cmp_worker.activities.migration import copy_data, shift_traffic, verify_target
from cmp_worker.activities.network_preflight import build_network_preflight_activities
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from cmp_worker.workflows.data_migration import DataMigrationWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.pull_refresh import GcpPullSecretRefreshWorkflow
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
from cmp_worker.workflows.traffic import TrafficShiftWorkflow


def create_activity_worker(client: Client) -> Worker:
    preflight = build_network_preflight_activities()
    return Worker(
        client,
        task_queue=MIGRATION_QUEUE,
        activities=[
            copy_data,
            shift_traffic,
            verify_target,
            traffic.traffic_prepare,
            traffic.traffic_set_weight,
            traffic.traffic_observe,
            traffic.traffic_rollback,
            preflight.run_network_preflight,  # MNP-01: call before initial copy/CDC
            # specs/data-migration: CDC (kubectl exec psql) and DB cutover (cmp-cutover Pod)
            data_migration.dm_precheck,
            data_migration.dm_prepare,
            data_migration.dm_copy_schema,
            data_migration.dm_subscribe,
            data_migration.dm_sample,
            data_migration.dm_readiness,
            data_migration.dm_cleanup,
            data_cutover.dm_install_cutover,
            data_cutover.dm_route_app,
            data_cutover.dm_switchover,
            data_cutover.dm_announce_cutover,
        ],
    )


def create_workflow_worker(client: Client) -> Worker:
    # Imported here: test modules defining Workflows import this factory, and the Workflow sandbox
    # must not re-import the API settings (`.env` read) that the event store pulls in.
    from cmp_worker.events import emit_migration_event

    return Worker(
        client,
        task_queue=WORKFLOW_QUEUE,
        workflows=[
            MigrationWorkflow,
            ProvisioningApprovalWorkflow,
            AnalysisWorkflow,
            SandboxVerificationWorkflow,
            TrafficShiftWorkflow,
            GcpPullSecretRefreshWorkflow,  # ticket 29
            DataMigrationWorkflow,
        ],
        # Ticket 49 F7: the MigrationWorkflow failure report runs where the Workflow runs, so it
        # never waits for another worker. DB writes only; no provider I/O on this queue.
        activities=[emit_migration_event],
    )
