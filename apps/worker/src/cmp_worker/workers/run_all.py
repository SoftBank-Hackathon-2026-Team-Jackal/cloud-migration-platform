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
from cmp_worker.workflows.sandbox_expire import ensure_sandbox_expire_schedule


async def main() -> None:
    from cmp_api.db import SessionLocal
    from cmp_api.migration_events import DbEventEmitter

    configure_emitter(DbEventEmitter(SessionLocal))  # ticket 24: Activities emit to the DB
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await ensure_refresh_schedule(client)  # ticket 29: GKE ECR pull Secret refresh
    await ensure_sandbox_expire_schedule(client)  # ticket 32: expired Sandbox sweep
    workers = [
        analysis_worker(client),
        sandbox_worker(client),
        build_worker(client),
        provisioning_worker(client),
        create_activity_worker(client),
        create_workflow_worker(client),
    ]
    await asyncio.gather(*(worker.run() for worker in workers))


if __name__ == "__main__":
    asyncio.run(main())
