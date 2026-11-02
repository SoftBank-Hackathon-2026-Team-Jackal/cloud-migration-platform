from cmp_contracts.queues import ANALYSIS_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.analysis import analyze_project


def create_worker(client: Client) -> Worker:
    return Worker(client, task_queue=ANALYSIS_QUEUE, activities=[analyze_project])
