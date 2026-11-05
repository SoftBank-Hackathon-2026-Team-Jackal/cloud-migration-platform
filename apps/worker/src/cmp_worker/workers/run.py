"""Run one Task Queue worker independently."""

import argparse
import asyncio

from temporalio.client import Client

from cmp_worker.config import settings
from cmp_worker.events import configure_emitter
from cmp_worker.workers.analysis import create_worker as analysis_worker
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
from cmp_worker.workers.provisioning import create_worker as provisioning_worker
from cmp_worker.workers.sandbox import create_worker as sandbox_worker
from cmp_worker.workflows.pull_refresh import ensure_refresh_schedule

WORKER_FACTORIES = {
    "workflow": create_workflow_worker,
    "analysis": analysis_worker,
    "sandbox": sandbox_worker,
    "build": build_worker,
    "provisioning": provisioning_worker,
    "migration": create_activity_worker,
}


async def main(queue: str) -> None:
    from cmp_api.db import SessionLocal
    from cmp_api.migration_events import DbEventEmitter

    configure_emitter(DbEventEmitter(SessionLocal))  # ticket 24: Activities emit to the DB
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    if queue == "workflow":
        await ensure_refresh_schedule(client)  # ticket 29: GKE ECR pull Secret refresh
    await WORKER_FACTORIES[queue](client).run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one Temporal worker boundary")
    parser.add_argument("--queue", required=True, choices=WORKER_FACTORIES)
    args = parser.parse_args()
    asyncio.run(main(args.queue))
