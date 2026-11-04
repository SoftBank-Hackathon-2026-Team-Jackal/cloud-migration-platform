from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.migration import copy_data, shift_traffic, verify_target
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow


def create_activity_worker(client: Client) -> Worker:
    return Worker(
        client, task_queue=MIGRATION_QUEUE, activities=[copy_data, shift_traffic, verify_target]
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
        ],
    )
