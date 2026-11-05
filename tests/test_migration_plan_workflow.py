"""Ticket 18 P7/P8 seam: MigrationWorkflow planning → approval child → execution result.

Real MigrationWorkflow, ProvisioningApprovalWorkflow and the API's Signal-With-Start delivery on
the Temporal time-skipping server; Provisioning Activities are fakes that record calls.
"""

import asyncio
import os

import pytest
from cmp_api.approval_delivery import TemporalApprovalDelivery
from cmp_contracts.queues import PROVISIONING_QUEUE, WORKFLOW_QUEUE, approval_workflow_id
from cmp_worker.workers.analysis import create_worker as analysis_worker
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
from cmp_worker.workers.sandbox import create_worker as sandbox_worker
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

PLAN = {"plan_id": "plan-test", "revision": 1}


class FakeProvisioning:
    def __init__(self, publish=None, decision="APPROVE"):
        self.publish = publish or {
            "status": "PUBLISHED",
            "code": "PLAN_PUBLISHED",
            **PLAN,
            "provider": "ON_PREM",
            "scope": "WORKLOADS",
            "manifest_digest": "a" * 64,
            "preview_digest": "b" * 64,
        }
        self.decision = decision
        self.calls = []

    def worker(self, client):
        @activity.defn(name="publish_migration_plan")
        async def publish(request: dict) -> dict:
            self.calls.append(("publish", request["migration_id"]))
            return self.publish

        @activity.defn(name="claim_approved_execution")
        async def claim(approval_id: str) -> dict:
            self.calls.append(("claim", approval_id))
            if self.decision == "REJECT":
                return {
                    **PLAN,
                    "approval_id": approval_id,
                    "status": "REJECTED",
                    "code": "PLAN_REJECTED",
                }
            return {
                **PLAN,
                "approval_id": approval_id,
                "status": "CLAIMED",
                "code": "EXECUTION_CLAIMED",
                "provider": "ON_PREM",
                "scope": "WORKLOADS",
                "manifest_digest": "a" * 64,
            }

        @activity.defn(name="execute_on_prem_plan")
        async def execute(request: dict) -> dict:
            self.calls.append(("execute", request["plan_id"]))
            return {"status": "SUCCEEDED", "code": "WORKLOADS_READY", "resource_refs": {}}

        @activity.defn(name="complete_approved_execution")
        async def complete(result: dict) -> dict:
            self.calls.append(("complete", result["status"]))
            return {"status": result["status"], "code": result["code"]}

        @activity.defn(name="provision_target")
        async def scaffold(project_id: str) -> dict:
            self.calls.append(("scaffold_provision", project_id))
            return {}

        return Worker(
            client,
            task_queue=PROVISIONING_QUEUE,
            activities=[publish, claim, execute, complete, scaffold],
        )

    def names(self):
        return [name for name, _ in self.calls]


async def wait_phase(handle, phase):
    for _ in range(200):
        state = await handle.query("status")
        if state["phase"] == phase:
            return state
        await asyncio.sleep(0.05)
    pytest.fail(f"phase {phase} not reached: {state}")


async def run_scenario(fake, scenario):
    async with await WorkflowEnvironment.start_time_skipping(  # noqa: SIM117
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        async with (
            analysis_worker(env.client),
            sandbox_worker(env.client),
            build_worker(env.client),
            fake.worker(env.client),
            create_activity_worker(env.client),
            create_workflow_worker(env.client),
        ):
            handle = await env.client.start_workflow(
                MigrationWorkflow.run, "project", id="migration-1", task_queue=WORKFLOW_QUEUE
            )
            outcome = await scenario(env, handle)
            history = await handle.fetch_history()
            await Replayer(
                workflows=[
                    MigrationWorkflow,
                    AnalysisWorkflow,
                    SandboxVerificationWorkflow,
                    ProvisioningApprovalWorkflow,
                ]
            ).replay_workflow(history)
            return outcome


def deliver(env, approval_id="approval-1"):
    return TemporalApprovalDelivery(env.client).deliver(approval_id=approval_id, **PLAN)


async def test_published_plan_waits_for_api_approval_then_executes():
    fake = FakeProvisioning()

    async def scenario(env, handle):
        state = await wait_phase(handle, "awaiting_approval")
        assert state["plan"] == PLAN and state["plan_code"] == "PLAN_PUBLISHED"
        assert "claim" not in fake.names()
        await handle.signal("approve")  # the legacy boolean Signal is not an approval
        await asyncio.sleep(0.2)
        assert "claim" not in fake.names()
        await deliver(env)
        return await handle.result()

    assert await run_scenario(fake, scenario) == "scaffold_complete"
    assert fake.calls[0] == ("publish", "migration-1")
    assert fake.names()[1:] == ["claim", "execute", "complete"]
    assert "scaffold_provision" not in fake.names()


async def test_rejected_plan_never_executes():
    fake = FakeProvisioning(decision="REJECT")

    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await deliver(env)
        return await handle.result()

    assert await run_scenario(fake, scenario) == "plan_rejected"
    assert "execute" not in fake.names()


async def test_expired_approval_never_executes_even_if_approved_later():
    fake = FakeProvisioning()

    async def scenario(env, handle):
        result = await handle.result()  # time-skipping passes the approval window
        await deliver(env)  # a late approval reaches a closed Revision Workflow: no-op
        child = env.client.get_workflow_handle(approval_workflow_id(**PLAN))
        assert (await child.result())["status"] == "EXPIRED"
        state = await handle.query("status")
        assert state["plan_code"] == "APPROVAL_EXPIRED"
        return result

    assert await run_scenario(fake, scenario) == "approval_expired"
    assert fake.names() == ["publish"]


async def test_publish_failure_stops_before_approval():
    failed = {"status": "FAILED", "code": "AGENT_REVOKED", "issues": ["AGENT_REVOKED"]}
    fake = FakeProvisioning(publish=failed)

    async def scenario(env, handle):
        result = await handle.result()
        assert (await handle.query("status"))["plan_code"] == "AGENT_REVOKED"
        return result

    assert await run_scenario(fake, scenario) == "plan_failed"
    assert fake.names() == ["publish"]


async def test_cancel_while_awaiting_approval_cancels_without_execution():
    fake = FakeProvisioning()

    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await handle.signal("request_cancel")
        result = await handle.result()
        child = env.client.get_workflow_handle(approval_workflow_id(**PLAN))
        assert (await child.result())["status"] == "CANCELLED"
        return result

    assert await run_scenario(fake, scenario) == "cancelled"
    assert fake.names() == ["publish"]


async def test_unconfigured_planning_keeps_the_scaffold_path():
    fake = FakeProvisioning(publish={"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"})

    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await handle.signal("approve")
        return await handle.result()

    assert await run_scenario(fake, scenario) == "scaffold_complete"
    assert fake.names() == ["publish", "scaffold_provision"]
