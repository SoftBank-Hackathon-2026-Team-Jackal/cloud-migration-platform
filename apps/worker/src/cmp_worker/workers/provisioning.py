from cmp_contracts.queues import PROVISIONING_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.provisioning import prepare_plan, provision_target


def create_worker(client: Client) -> Worker:
    return Worker(
        client, task_queue=PROVISIONING_QUEUE, activities=[prepare_plan, provision_target]
    )
