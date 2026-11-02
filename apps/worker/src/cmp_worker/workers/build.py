from cmp_contracts.queues import BUILD_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.build import prepare_image


def create_worker(client: Client) -> Worker:
    return Worker(client, task_queue=BUILD_QUEUE, activities=[prepare_image])
