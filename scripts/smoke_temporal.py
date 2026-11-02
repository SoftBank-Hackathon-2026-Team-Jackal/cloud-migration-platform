"""Exercise the real local Temporal server with inert scaffold Activities."""

import asyncio
from uuid import uuid4

from cmp_contracts.queues import WORKFLOW_NAME, WORKFLOW_QUEUE
from cmp_worker.config import settings
from cmp_worker.workers.analysis import create_worker as analysis_worker
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
from cmp_worker.workers.provisioning import create_worker as provisioning_worker
from temporalio.client import Client


async def main() -> None:
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    workers = [
        analysis_worker(client),
        build_worker(client),
        provisioning_worker(client),
        create_activity_worker(client),
        create_workflow_worker(client),
    ]
    async with asyncio.TaskGroup() as group:
        for worker in workers:
            group.create_task(worker.run())
        handle = await client.start_workflow(
            WORKFLOW_NAME, "smoke-test", id=f"smoke-{uuid4()}", task_queue=WORKFLOW_QUEUE
        )
        try:
            for _ in range(50):
                state = await handle.query("status")
                if state["phase"] == "awaiting_approval":
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError(f"Approval gate not reached: {state}")
            await handle.signal("approve")
            result = await asyncio.wait_for(handle.result(), timeout=20)
            assert result == "scaffold_complete", result
            print(f"Temporal scaffold passed: {handle.id} → {result}")
        finally:
            for worker in workers:
                await worker.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
