from cmp_contracts.queues import ANALYSIS_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.analysis import (
    analyze_project,
    build_requirements,
    check_evidence,
    fetch_source,
    map_usage,
    precheck,
    record_review,
    scan_aws_infra,
    scan_code,
    scan_manifests,
    scan_network,
)


def create_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=ANALYSIS_QUEUE,
        activities=[
            analyze_project,
            fetch_source,
            scan_code,
            scan_manifests,
            scan_aws_infra,
            scan_network,
            build_requirements,
            precheck,
            map_usage,
            check_evidence,
            record_review,
        ],
    )
