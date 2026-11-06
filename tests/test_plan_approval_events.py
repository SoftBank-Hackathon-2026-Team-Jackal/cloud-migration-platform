"""Ticket 60 seam: real-mode planning/approval events in the web script order.

`publish_migration_plan` (real `PlanPublisher`, ON_PREM, SQLite + local ArtifactStore) emits
`plan.step`* → `plan.preview` → `plan.ready` → `approval.required`; a REJECTed Revision is never
reused, so the next publication is Revision + 1; `MigrationWorkflow` republishes after REJECT and
waits for the new Revision (Temporal time-skipping server, fake Provisioning Activities).
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
import test_plan_publish as plan_publish
from cmp_api.approvals import Approver, PlanApproval, latest_revision, record_decision
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.analysis import AnalysisReport, MappingRow
from cmp_contracts.events import validate_data
from cmp_contracts.queues import PROVISIONING_QUEUE, WORKFLOW_QUEUE
from cmp_worker.events import configure_emitter
from cmp_worker.provisioning_events import PlanEvents, plan_step
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_migration_plan_workflow import handover_fake, traffic_handoff

# Shared ticket 18 fixtures and helpers (ON_PREM publication on SQLite + local ArtifactStore).
db, store, onprem = plan_publish.db, plan_publish.store, plan_publish.onprem
platform_registry = plan_publish.platform_registry
MIGRATION, publisher, register = (
    plan_publish.MIGRATION,
    plan_publish.publisher,
    plan_publish.register,
)
registration, stored = plan_publish.registration, plan_publish.stored
REPORT = Path(__file__).parent / "fixtures/analysis/analysis-report-eks-k3s.json"
ORDER = ["plan.step", "plan.step", "plan.preview", "plan.ready", "approval.required"]


@pytest.fixture
def emitted():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


def report() -> AnalysisReport:
    return AnalysisReport.model_validate(json.loads(REPORT.read_text())["report"])


def with_report(db, store, content: bytes | None = None):
    """Register the ON_PREM input whose sandbox `report_ref` names the stored analysis report."""
    content = content if content is not None else report().model_dump_json().encode()
    ref = store.put(content, ref_id="report-1", kind="analysis-report")
    reg, body = registration()
    body["planning_request"]["sandbox"]["report_ref"] = {
        "id": "report-1",
        "version": "1",
        "sha256": ref["sha256"],
    }
    register(db, store, type(reg).model_validate(body))


def events_of(emitter, migration_id=MIGRATION):
    return [e for e in emitter.events if e.migration_id == migration_id]


async def publish(wrapped, migration_id=MIGRATION) -> dict:
    @activity.defn(name="publish_migration_plan")
    async def probe() -> dict:
        return wrapped.publish(migration_id)

    return await ActivityEnvironment().run(probe)


def reject(db, result):
    with db() as session:
        session.add(Approver(id="demo", credential_digest="0" * 64, migration_ids=[MIGRATION]))
        session.commit()
        record_decision(
            session,
            approver=session.get(Approver, "demo"),
            plan_id=result["plan_id"],
            revision=result["revision"],
            decision="REJECT",
            scope=result["scope"],
            manifest_digest=result["manifest_digest"],
            preview_digest=result["preview_digest"],
        )


# --- pure mapping ----------------------------------------------------------------------------


def test_mapping_row_becomes_a_web_plan_step():
    rows = report().mapping
    assert plan_step(rows[0]) == {
        "id": "postgres-advisory-lock",
        "group": "code",
        "grade": "PARTIAL",
        "before": "postgres",
        "after": "",
        "evidence": "app/tasks.py:12",
        "change": "advisory locks",
        "action": "APPROVAL",
        "keep_source": False,
        "verification": "EVIDENCE_CHECKED",
    }
    infra = MappingRow.model_validate(
        {
            **rows[1].model_dump(),
            "treatment": None,
            "evidence": [
                {"kind": "MANIFEST", "locator": "k8s/a.yaml"},
                {"kind": "CODE", "locator": "app/b.py:3"},
            ],
        }
    )
    step = plan_step(infra)
    assert (step["group"], step["action"], step["keep_source"]) == (
        "infrastructure",
        "APPROVAL",
        True,
    )
    assert step["evidence"] == "k8s/a.yaml, app/b.py:3"
    validate_data("plan.step", step)


# --- publication events ----------------------------------------------------------------------


async def test_publication_emits_steps_preview_ready_then_approval_required(
    db, store, tmp_path, emitted, onprem
):
    with_report(db, store)
    result = await publish(PlanEvents(publisher(db, store, tmp_path)))
    assert result["status"] == "PUBLISHED"
    events = events_of(emitted)
    assert [e.type for e in events] == ORDER
    assert [e.data["id"] for e in events[:2]] == ["postgres-advisory-lock", "s3-object-read"]
    assert {e.phase for e in events[:4]} == {"planning"}

    preview = events[2].data
    assert (preview["plan_id"], preview["revision"]) == (result["plan_id"], 1)
    assert preview["manifest_digest"] == result["manifest_digest"]
    assert preview["changes"] and preview["target"] == {
        "provider": "ON_PREM",
        "platform": "K3s",
        "location": "onprem-prod-001",
    }
    rules = preview["security_rules"]
    assert rules[:2] == [
        {"from": "ingress", "to": "api-main", "port": 8080, "protocol": "TCP", "action": "ALLOW"},
        {"from": "api-main", "to": "postgres-main", "port": 5432, "protocol": "TCP",
         "action": "ALLOW"},
    ]  # fmt: skip
    assert rules[-1] == {"from": "*", "to": "*", "port": None, "protocol": "ANY", "action": "DENY"}

    ready = events[3].data
    assert ready["revision"] == 1 and ready["preview_digest"] == result["preview_digest"]

    required = events[4]
    assert (required.phase, required.status) == ("awaiting_approval", "waiting")
    assert required.data["excluded"] == ["s3-object-read"]  # KEEP rows stay on the source
    assert required.data["rollback_policy"] == ""
    # Only the reviewed, stored Preview (names and refs, no Secret values) plus identifiers.
    _, stored_preview = stored(db, result)
    extra = {"plan_id", "revision", "target", "security_rules"}
    assert {k: v for k, v in preview.items() if k not in extra} == stored_preview.model_dump(
        mode="json", exclude_none=True
    ) | {"changes": preview["changes"], "applied_defaults": preview["applied_defaults"]}


async def test_missing_analysis_report_still_emits_preview_ready_and_required(
    db, store, tmp_path, emitted, onprem
):
    await publish(PlanEvents(publisher(db, store, tmp_path)))
    types = [e.type for e in events_of(emitted)]
    assert types == ["plan.preview", "plan.ready", "approval.required"]
    assert [log["code"] for log in emitted.logs] == ["PLAN_STEP_SOURCE_UNAVAILABLE"]


async def test_failed_publication_emits_nothing(db, store, tmp_path, emitted):
    result = await publish(PlanEvents(publisher(db, store, tmp_path)))
    assert result["code"] == "PLANNING_INPUT_MISSING"
    assert events_of(emitted) == []


async def test_reused_revision_emits_the_same_values(db, store, tmp_path, onprem):
    """A retried publish (Worker lost after publishing) still reaches the screen, same values."""
    wrapped = PlanEvents(publisher(db, store, tmp_path))
    runs = []
    for _ in range(2):
        emitter = InMemoryEmitter()  # a new Activity attempt has new idempotency keys
        previous = configure_emitter(emitter)
        try:
            runs.append((await publish(wrapped), events_of(emitter)))
        finally:
            configure_emitter(previous)
    (_, first), (again, second) = runs
    assert again["code"] == "PLAN_REUSED"
    assert [e.type for e in second] == ORDER[2:]
    assert [e.data for e in first] == [e.data for e in second]


# --- REJECT → new Revision -------------------------------------------------------------------


async def test_rejected_revision_is_republished_as_the_next_revision(
    db, store, tmp_path, emitted, onprem
):
    wrapped = PlanEvents(publisher(db, store, tmp_path))
    first = await publish(wrapped)
    reject(db, first)
    second = await publish(wrapped)
    assert (second["code"], second["revision"]) == ("PLAN_PUBLISHED", 2)
    with db() as session:
        assert latest_revision(session, first["plan_id"]) == 2
        assert session.get(PlanApproval, session.scalar(_approval_id())).revision == 1
    ready = [e.data["revision"] for e in emitted.of_type("plan.ready")]
    required = emitted.of_type("approval.required")
    assert ready == [1, 2] and len(required) == 2
    # the same input after the new Revision is reused again (P3 unchanged)
    assert (await publish(wrapped))["code"] == "PLAN_REUSED"


def _approval_id():
    from sqlalchemy import select

    return select(PlanApproval.id)


# --- Workflow: REJECT republishes and waits for the new Revision -----------------------------


class Republishing:
    """Fake Provisioning: Revision N per publication, decisions per delivered approval."""

    def __init__(self, decisions: dict[str, str], revisions=(1, 2)):
        self.decisions = decisions
        self.revisions = list(revisions)
        self.calls = []

    def published(self, revision: int) -> dict:
        return {
            "status": "PUBLISHED",
            "code": "PLAN_PUBLISHED",
            "plan_id": "plan-test",
            "revision": revision,
            "provider": "ON_PREM",
            "scope": "WORKLOADS",
            "manifest_digest": "a" * 64,
            "preview_digest": "b" * 64,
        }

    def worker(self, client):
        @activity.defn(name="publish_migration_plan")
        async def publish(request: dict) -> dict:
            revision = self.revisions[min(len(self.calls_of("publish")), len(self.revisions) - 1)]
            self.calls.append(("publish", revision))
            return self.published(revision)

        @activity.defn(name="claim_approved_execution")
        async def claim(approval_id: str) -> dict:
            decision, revision = self.decisions[approval_id]
            self.calls.append(("claim", revision))
            base = {"plan_id": "plan-test", "revision": revision, "approval_id": approval_id}
            if decision == "REJECT":
                return {**base, "status": "REJECTED", "code": "PLAN_REJECTED"}
            return {
                **base,
                "status": "CLAIMED",
                "code": "EXECUTION_CLAIMED",
                "provider": "ON_PREM",
                "scope": "WORKLOADS",
                "manifest_digest": "a" * 64,
            }

        @activity.defn(name="execute_on_prem_plan")
        async def execute(request: dict) -> dict:
            self.calls.append(("execute", request["revision"]))
            return {"status": "SUCCEEDED", "code": "WORKLOADS_READY", "resource_refs": {}}

        @activity.defn(name="complete_approved_execution")
        async def complete(result: dict) -> dict:
            return {"status": result["status"], "code": result["code"]}

        self.traffic = traffic_handoff({"plan_id": "plan-test", "revision": 2})
        self.handover_requests = []
        return Worker(
            client,
            task_queue=PROVISIONING_QUEUE,
            activities=[publish, claim, execute, complete, handover_fake(self)],
        )

    def calls_of(self, name):
        return [c for c in self.calls if c[0] == name]


async def run_migration(fake, scenario):
    from cmp_worker.workers.analysis import create_worker as analysis_worker
    from cmp_worker.workers.build import create_worker as build_worker
    from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
    from cmp_worker.workers.sandbox import create_worker as sandbox_worker
    from cmp_worker.workflows.analysis import AnalysisWorkflow
    from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
    from cmp_worker.workflows.migration import MigrationWorkflow
    from cmp_worker.workflows.sandbox import SandboxVerificationWorkflow

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


async def wait_revision(handle, revision):
    for _ in range(200):
        state = await handle.query("status")
        if state["phase"] == "awaiting_approval" and state["plan"]["revision"] == revision:
            return state
        await asyncio.sleep(0.05)
    pytest.fail(f"revision {revision} never awaited approval: {state}")


def deliver(env, approval_id, revision):
    from cmp_api.approval_delivery import TemporalApprovalDelivery

    return TemporalApprovalDelivery(env.client).deliver(
        approval_id=approval_id, plan_id="plan-test", revision=revision
    )


async def test_reject_republishes_and_the_new_revision_can_be_approved():
    fake = Republishing({"approval-1": ("REJECT", 1), "approval-2": ("APPROVE", 2)})

    async def scenario(env, handle):
        await wait_revision(handle, 1)
        await deliver(env, "approval-1", 1)
        state = await wait_revision(handle, 2)
        assert state["plan_code"] == "PLAN_PUBLISHED"
        await deliver(env, "approval-2", 2)
        return await handle.result()

    assert await run_migration(fake, scenario) == "scaffold_complete"
    assert fake.calls == [
        ("publish", 1),
        ("claim", 1),
        ("publish", 2),
        ("claim", 2),
        ("execute", 2),
    ]


async def test_republish_that_returns_the_rejected_revision_ends_rejected():
    fake = Republishing({"approval-1": ("REJECT", 1)}, revisions=(1,))

    async def scenario(env, handle):
        await wait_revision(handle, 1)
        await deliver(env, "approval-1", 1)
        return await handle.result()

    assert await run_migration(fake, scenario) == "plan_rejected"
    assert [c[0] for c in fake.calls] == ["publish", "claim", "publish"]


async def test_third_reject_ends_the_migration_rejected():
    decisions = {f"approval-{n}": ("REJECT", n) for n in (1, 2, 3)}
    fake = Republishing(decisions, revisions=(1, 2, 3, 4))

    async def scenario(env, handle):
        for n in (1, 2, 3):
            await wait_revision(handle, n)
            await deliver(env, f"approval-{n}", n)
        result = await handle.result()
        assert (await handle.query("status"))["plan_code"] == "PLAN_REJECTED"
        return result

    assert await run_migration(fake, scenario) == "plan_rejected"
    assert [c for c in fake.calls if c[0] == "publish"] == [("publish", n) for n in (1, 2, 3)]


# --- plan.ready carries the scope the approval API compares ----------------------------------

client, approver = plan_publish.client, plan_publish.approver


def _decide(client, headers, ready, decision, scope):
    return client.post(
        f"/provisioning/plans/{ready['plan_id']}/revisions/{ready['revision']}/approvals",
        json={
            "decision": decision,
            "scope": scope,
            "manifest_digest": ready["manifest_digest"],
            "preview_digest": ready["preview_digest"],
        },
        headers=headers,
    )


async def _onprem_ready(db, store, tmp_path, emitted) -> dict:
    with_report(db, store)
    await publish(PlanEvents(publisher(db, store, tmp_path)))
    [ready] = emitted.of_type("plan.ready")
    assert validate_data("plan.ready", ready.data) == ready.data
    return ready.data


@pytest.mark.parametrize("decision", ["APPROVE", "REJECT"])
async def test_plan_ready_scope_is_accepted_by_the_approval_api(
    db, store, tmp_path, emitted, onprem, client, decision
):
    ready = await _onprem_ready(db, store, tmp_path, emitted)
    assert ready["scope"] == "WORKLOADS"  # ON_PREM Plans are always WORKLOADS
    headers = approver(client)
    # The approval rule is unchanged: any other scope is still a target mismatch.
    full = _decide(client, headers, ready, decision, "FULL")
    assert full.status_code == 409 and "APPROVAL_TARGET_MISMATCH" in full.text
    accepted = _decide(client, headers, ready, decision, ready["scope"])
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["scope"] == "WORKLOADS"
