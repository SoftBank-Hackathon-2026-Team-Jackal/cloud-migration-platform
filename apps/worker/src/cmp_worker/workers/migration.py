from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities import traffic
from cmp_worker.activities.migration import copy_data, shift_traffic, verify_target
from cmp_worker.activities.network_preflight import build_network_preflight_activities
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
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
        ],
    )


def create_workflow_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=WORKFLOW_QUEUE,
        workflows=[
            MigrationWorkflow,
            ProvisioningApprovalWorkflow,
            AnalysisWorkflow,
            SandboxVerificationWorkflow,
            TrafficShiftWorkflow,
        ],
    )
