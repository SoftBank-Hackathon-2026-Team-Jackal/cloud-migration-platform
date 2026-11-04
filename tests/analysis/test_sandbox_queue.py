from datetime import timedelta

import pytest
from cmp_contracts.queues import SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker.workers.sandbox import create_worker
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@workflow.defn
class SandboxQueueProbeWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            "sandbox_queue_probe",
            task_queue=SANDBOX_QUEUE,
            start_to_close_timeout=timedelta(seconds=5),
        )


@pytest.mark.asyncio
async def test_sandbox_worker_processes_probe_on_its_own_queue():
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[SandboxQueueProbeWorkflow]),
        create_worker(env.client),
    ):
        result = await env.client.execute_workflow(
            SandboxQueueProbeWorkflow.run,
            id="sandbox-queue-test",
            task_queue=WORKFLOW_QUEUE,
        )
    assert result == "scaffold"


def test_sandbox_worker_is_available_for_individual_execution():
    from cmp_worker.workers import run, run_all

    assert run.WORKER_FACTORIES["sandbox"] is create_worker
    assert run_all.sandbox_worker is create_worker
    assert {"analysis", "build", "provisioning", "migration", "workflow"} <= set(
        run.WORKER_FACTORIES
    )


@pytest.mark.asyncio
async def test_run_all_starts_sandbox_with_existing_workers(monkeypatch):
    from cmp_worker.workers import run_all

    started = set()
    client = object()

    class FakeWorker:
        def __init__(self, name):
            self.name = name

        async def run(self):
            started.add(self.name)

    async def connect(*args, **kwargs):
        return client

    monkeypatch.setattr(run_all.Client, "connect", connect)
    for name in (
        "analysis_worker",
        "build_worker",
        "provisioning_worker",
        "sandbox_worker",
        "create_activity_worker",
        "create_workflow_worker",
    ):
        monkeypatch.setattr(run_all, name, lambda received, name=name: FakeWorker(name))
    await run_all.main()
    assert started == {
        "analysis_worker",
        "build_worker",
        "provisioning_worker",
        "sandbox_worker",
        "create_activity_worker",
        "create_workflow_worker",
    }
