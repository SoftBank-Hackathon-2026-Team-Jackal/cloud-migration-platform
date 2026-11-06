from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities import data_cutover, data_migration, data_tunnel, traffic
from cmp_worker.activities.migration import copy_data, shift_traffic, verify_target
from cmp_worker.activities.network_preflight import build_network_preflight_activities
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from cmp_worker.workflows.cloud_teardown import CloudTeardownWorkflow
from cmp_worker.workflows.connection_check import GcpConnectionCheckWorkflow
from cmp_worker.workflows.data_migration import DataMigrationWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.pull_refresh import GcpPullSecretRefreshWorkflow
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
from cmp_worker.workflows.sandbox_expire import SandboxExpireWorkflow
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
            data_tunnel.dm_tunnel,  # ticket 10: WireGuard 복제 경로
            data_tunnel.dm_tunnel_cleanup,  # Provisioning 67: 사용 종료 뒤 명시적 정리
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
            SandboxExpireWorkflow,  # ticket 32
            CloudTeardownWorkflow,  # ticket 53
            GcpConnectionCheckWorkflow,  # ticket 48
            DataMigrationWorkflow,
        ],
        # Ticket 49 F7: the MigrationWorkflow failure report runs where the Workflow runs, so it
        # never waits for another worker. DB writes only; no provider I/O on this queue.
        # #234: `traffic_readiness` only queries the data migration child (no provider I/O).
        activities=[emit_migration_event, traffic.traffic_readiness],
    )
