"""Worker for the Sandbox activity queue."""

from cmp_contracts.queues import SANDBOX_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.sandbox import sandbox_queue_probe


def create_worker(client: Client) -> Worker:
    return Worker(client, task_queue=SANDBOX_QUEUE, activities=[sandbox_queue_probe])
