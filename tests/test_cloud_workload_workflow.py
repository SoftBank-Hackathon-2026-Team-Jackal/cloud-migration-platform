"""Ticket 22 W8 seam: MigrationWorkflow AWS/GCP infra approval → workload Plan → approval → 09/11.

Real MigrationWorkflow, ProvisioningApprovalWorkflow and the API's Signal-With-Start delivery on
the Temporal time-skipping server; Provisioning Activities are fakes that record calls. The old
history fixture was recorded with the pre-ticket-22 code (infra execution, then scaffold steps).
"""

import asyncio
from pathlib import Path

import pytest
from cmp_api.approval_delivery import TemporalApprovalDelivery
from cmp_contracts.queues import PROVISIONING_QUEUE
from cmp_worker.workflows.analysis import AnalysisWorkflow
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer, Worker
from test_migration_plan_workflow import handover_fake, run_scenario, traffic_handoff, wait_phase

OLD_HISTORY = Path(__file__).parent / "fixtures/temporal/migration-aws-infra-only-v1.json"
INFRA = {"plan_id": "plan-infra", "revision": 1}
WORK = {"plan_id": "plan-work", "revision": 1}
STEPS = {
    ("AWS", "INFRASTRUCTURE"): "execute_aws_plan",
    ("AWS", "WORKLOADS"): "execute_aws_workloads",
    ("GCP", "INFRASTRUCTURE"): "execute_gcp_plan",
    ("GCP", "WORKLOADS"): "execute_gcp_workloads",
}


def published(plan, provider, scope):
    return {
        "status": "PUBLISHED",
        "code": "PLAN_PUBLISHED",
        **plan,
        "provider": provider,
        "scope": scope,
        "manifest_digest": "a" * 64,
        "preview_digest": "b" * 64,
    }


class FakeCloudProvisioning:
    def __init__(self, provider="AWS", workloads=None, reject=()):
        self.provider = provider
        self.workloads = workloads or published(WORK, provider, "WORKLOADS")
        self.reject = set(reject)
        self.calls = []
        self.traffic = traffic_handoff(WORK)  # ticket 50
        self.handover_requests = []

    def worker(self, client):
        plans = {"approval-infra": (INFRA, "INFRASTRUCTURE"), "approval-work": (WORK, "WORKLOADS")}

        @activity.defn(name="publish_migration_plan")
        async def publish(request: dict) -> dict:
            self.calls.append(("publish", request))
            return published(INFRA, self.provider, "INFRASTRUCTURE")

        @activity.defn(name="publish_workload_plan")
        async def publish_workloads(request: dict) -> dict:
            self.calls.append(("publish_workloads", request))
            return self.workloads

        @activity.defn(name="claim_approved_execution")
        async def claim(approval_id: str) -> dict:
            plan, scope = plans[approval_id]
            self.calls.append(("claim", approval_id))
            if approval_id in self.reject:
                return {
                    **plan,
                    "approval_id": approval_id,
                    "status": "REJECTED",
                    "code": "PLAN_REJECTED",
                }
            return {
                **plan,
                "approval_id": approval_id,
                "status": "CLAIMED",
                "code": "EXECUTION_CLAIMED",
                "provider": self.provider,
                "scope": scope,
                "manifest_digest": "a" * 64,
            }

        def executor(name):
            @activity.defn(name=name)
            async def execute(request: dict) -> dict:
                self.calls.append((name, request["plan_id"]))
                return {"status": "SUCCEEDED", "code": "OK", "resource_refs": {"k": "v"}}

            return execute

        @activity.defn(name="complete_approved_execution")
        async def complete(result: dict) -> dict:
            self.calls.append(("complete", result["approval_id"]))
            return {"status": result["status"], "code": result["code"]}

        return Worker(
            client,
            task_queue=PROVISIONING_QUEUE,
            activities=[
                publish,
                publish_workloads,
                claim,
                complete,
                handover_fake(self),
                *(executor(name) for name in STEPS.values()),
            ],
        )

    def names(self):
        return [name for name, _ in self.calls]


def deliver(env, approval_id, plan):
    return TemporalApprovalDelivery(env.client).deliver(approval_id=approval_id, **plan)


@pytest.mark.parametrize("provider", ["AWS", "GCP"])
async def test_cloud_infra_then_workload_plan_approval_runs_09_11(provider):
    fake = FakeCloudProvisioning(provider)

    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await deliver(env, "approval-infra", INFRA)
        for _ in range(200):  # infra executes, then the workload Plan waits for its approval
            state = await handle.query("status")
            if state["phase"] == "awaiting_approval" and state["plan"] == WORK:
                break
            await asyncio.sleep(0.05)
        assert state["infrastructure_plan"] == INFRA and state["plan_code"] == "PLAN_PUBLISHED"
        assert "execute_aws_workloads" not in fake.names()
        await deliver(env, "approval-work", WORK)
        result = await handle.result()
        assert (await handle.query("status"))["plan"] == WORK  # handoff reads this Revision
        return result

    assert await run_scenario(fake, scenario) == "scaffold_complete"
    infra_step, work_step = STEPS[(provider, "INFRASTRUCTURE")], STEPS[(provider, "WORKLOADS")]
    assert fake.names() == [
        "publish",
        "claim",
        infra_step,
        "complete",
        "publish_workloads",
        "claim",
        work_step,
        "complete",
    ]
    assert fake.calls[4][1] == {"migration_id": "migration-1", "infrastructure": INFRA}
    # Ticket 50: Traffic reads the workload Revision's result, not the infrastructure one.
    assert fake.handover_requests == [WORK]


async def test_workload_publish_failure_ends_plan_failed_without_deploying():
    failed = {"status": "FAILED", "code": "WORKLOAD_SETTINGS_MISSING", "issues": ["X"]}
    fake = FakeCloudProvisioning(workloads=failed)

    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await deliver(env, "approval-infra", INFRA)
        result = await handle.result()
        assert (await handle.query("status"))["plan_code"] == "WORKLOAD_SETTINGS_MISSING"
        return result

    assert await run_scenario(fake, scenario) == "plan_failed"
    assert fake.names()[-1] == "publish_workloads"
    assert "execute_aws_workloads" not in fake.names()


async def test_rejected_workload_plan_never_deploys():
    fake = FakeCloudProvisioning(reject={"approval-work"})

    async def scenario(env, handle):
        await wait_phase(handle, "awaiting_approval")
        await deliver(env, "approval-infra", INFRA)
        for _ in range(200):
            if (await handle.query("status"))["plan"] == WORK:
                break
            await asyncio.sleep(0.05)
        await deliver(env, "approval-work", WORK)
        return await handle.result()

    assert await run_scenario(fake, scenario) == "plan_rejected"
    assert "execute_aws_workloads" not in fake.names()


async def test_pre_ticket_22_cloud_history_replays_without_the_workload_step():
    history = WorkflowHistory.from_json("migration-1", OLD_HISTORY.read_text())
    await Replayer(
        workflows=[
            MigrationWorkflow,
            AnalysisWorkflow,
            SandboxVerificationWorkflow,
            ProvisioningApprovalWorkflow,
        ]
    ).replay_workflow(history)
