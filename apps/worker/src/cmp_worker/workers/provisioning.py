from cmp_api.db import SessionLocal
from cmp_contracts.queues import PROVISIONING_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.activities.provisioning import prepare_plan, provision_target


def create_worker(client: Client) -> Worker:
    # The approval executor is the safe record-only default; no Terraform/Helm/Agent is wired.
    approval = ApprovalActivities(SessionLocal)
    return Worker(
        client,
        task_queue=PROVISIONING_QUEUE,
        activities=[prepare_plan, provision_target, approval.execute_approved_plan],
    )
