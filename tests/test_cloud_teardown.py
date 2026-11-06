"""Ticket 53 (D1~D4, D11) seam: executed Plan Revision → cloud workload teardown Activity.

kubectl/Helm are a fake cluster and the business DB is SQLite; no cloud or cluster call is made.
"""

from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
import test_aws_apply as aws_case
from cmp_api.approvals import PlanExecution, publish_plan_revision
from cmp_contracts.plan import Preview
from cmp_contracts.queues import PROVISIONING_QUEUE, WORKFLOW_QUEUE
from cmp_providers.plans import manifest_digest
from cmp_worker.activities.cloud_teardown import CloudTeardownActivities, TeardownTimeouts
from cmp_worker.workflows.cloud_teardown import CloudTeardownWorkflow
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker
from test_aws_workloads import ACCOUNT, NAMESPACE, Plan, db  # noqa: F401 - db is a fixture

FAST = TeardownTimeouts(load_balancer_seconds=0, volume_seconds=0, interval_seconds=0)
CLUSTER_REFS = {
    "eks_cluster_name": "cmp-target",
    "vpc_id": "vpc-0123456789abcdef0",
    "lb_controller_role_arn": "arn:aws:iam::714066696240:role/cmp-target-lbc",
}


class FakeCluster:
    def __init__(self):
        self.calls = []
        self.releases = {(NAMESPACE, "cmp-demo-api"), (NAMESPACE, "cmp-demo-db")}
        self.lb = [("ingress", NAMESPACE, "cmp-demo-api"), ("service", "user-ns", "user-lb")]
        self.lb_sticky = False  # finalizer never released (controller cannot delete the LB)
        self.pvcs = {NAMESPACE: [{"name": "cmp-demo-db", "volume": "pv-db"}]}
        self.pvs = {
            "pv-db": {"name": "pv-db", "reclaim": "Retain", "handle": "vol-0db",
                      "namespace": NAMESPACE},
            "pv-user": {"name": "pv-user", "reclaim": "Retain", "handle": "vol-0user",
                        "namespace": "user-ns"},
        }  # fmt: skip
        self.csi_deletes = True

    def helm_uninstall(self, namespace, name):
        self.calls.append(("uninstall", namespace, name))
        self.releases.discard((namespace, name))

    def load_balancer_objects(self):
        return list(self.lb)

    def delete_object(self, kind, namespace, name):
        self.calls.append(("delete", kind, namespace, name))
        if kind == "pvc":
            self.pvcs[namespace] = [c for c in self.pvcs[namespace] if c["name"] != name]
            claimed = [pv for pv in self.pvs.values() if pv["namespace"] == namespace]
            for pv in claimed:
                if pv["reclaim"] == "Delete" and self.csi_deletes:
                    del self.pvs[pv["name"]]
        elif not self.lb_sticky:
            self.lb = [o for o in self.lb if o != (kind, namespace, name)]

    def claims(self, namespace):
        return list(self.pvcs.get(namespace, []))

    def persistent_volumes(self):
        return [dict(pv) for pv in self.pvs.values()]

    def set_reclaim_policy(self, volume, policy):
        self.calls.append(("reclaim", volume, policy))
        self.pvs[volume]["reclaim"] = policy


class FakeRuntime:
    def __init__(self, cluster, *, account=ACCOUNT, aws_profile="cmp-aws-prod", fail=False):
        self.cluster, self.account, self.aws_profile, self.fail = (
            cluster,
            account,
            aws_profile,
            fail,
        )
        self.profiles = []

    def with_profile(self, profile):
        self.profiles.append(profile)
        return self

    def account_id(self):
        return self.account

    @contextmanager
    def connect(self, target):
        if self.fail:
            raise RuntimeError("kubeconfig refused")
        self.target = target
        yield self.cluster


def executed(tmp_path, db, *, refs=CLUSTER_REFS, status="FAILED", credential_ref=None):  # noqa: F811
    """An executed INFRASTRUCTURE Revision and an approved WORKLOADS Plan of the same target."""
    workloads = Plan(tmp_path, db)
    request = aws_case.spec(
        environment_id="production-001",
        state_ref={"id": "state-001", "version": "1"},
        state_key="cmp/production-001/terraform.tfstate",
        **({"credential_ref": credential_ref} if credential_ref else {}),
    )
    manifest, _, _ = aws_case.artifacts(tmp_path, request)
    with db() as session:
        publish_plan_revision(
            session,
            manifest,
            Preview(
                contract_version="1",
                manifest_digest=manifest_digest(manifest),
                changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "c"}],
            ),
        )
        for plan_id in ("plan-aws", "plan-009"):
            session.add(
                PlanExecution(
                    plan_id=plan_id, revision=1, step="PROVISION",
                    approval_id=workloads.approval_id, status=status, started_at=1,
                    finished_at=2, resource_refs=refs if plan_id == "plan-aws" else {},
                )
            )  # fmt: skip
        session.commit()
    return {"plan_id": "plan-aws", "revision": 1}


async def teardown(db, tmp_path, runtime, request):  # noqa: F811
    activities = CloudTeardownActivities(
        db, artifact_root=tmp_path, aws_runtime=runtime, timeouts=FAST
    )
    return await ActivityEnvironment().run(activities.teardown_cloud_workloads, request)


async def test_order_is_helm_then_load_balancers_then_volumes_and_data_is_kept(tmp_path, db):  # noqa: F811
    cluster = FakeCluster()
    request = executed(tmp_path, db)
    result = await teardown(db, tmp_path, FakeRuntime(cluster), request)
    assert (result["status"], result["code"]) == ("SUCCEEDED", "WORKLOADS_REMOVED")
    assert cluster.calls[:2] == [
        ("uninstall", NAMESPACE, "cmp-demo-api"),
        ("uninstall", NAMESPACE, "cmp-demo-db"),
    ]
    assert cluster.calls[2:] == [
        ("delete", "ingress", NAMESPACE, "cmp-demo-api"),
        ("delete", "service", "user-ns", "user-lb"),  # every LB in the dedicated cluster
    ]
    refs = result["resource_refs"]
    assert refs["load_balancer_objects_deleted"] == "2"
    assert refs["retained_volume_ids"] == "vol-0db,vol-0user"  # D4: kept and reported
    assert "pv-db" in cluster.pvs and cluster.pvcs[NAMESPACE]


async def test_delete_data_lets_csi_delete_platform_disks_only(tmp_path, db):  # noqa: F811
    cluster = FakeCluster()
    request = executed(tmp_path, db) | {"delete_data": True}
    result = await teardown(db, tmp_path, FakeRuntime(cluster), request)
    assert result["code"] == "WORKLOADS_REMOVED"
    assert ("reclaim", "pv-db", "Delete") in cluster.calls
    assert ("delete", "pvc", NAMESPACE, "cmp-demo-db") in cluster.calls
    assert "pv-db" not in cluster.pvs
    assert cluster.pvs["pv-user"]["reclaim"] == "Retain"  # other Namespaces are never deleted
    assert result["resource_refs"]["retained_volume_ids"] == "vol-0user"


async def test_load_balancer_that_does_not_go_away_stops_before_volumes(tmp_path, db):  # noqa: F811
    cluster = FakeCluster()
    cluster.lb_sticky = True
    request = executed(tmp_path, db) | {"delete_data": True}
    result = await teardown(db, tmp_path, FakeRuntime(cluster), request)
    assert (result["status"], result["code"]) == ("UNKNOWN", "LOAD_BALANCER_DELETE_PENDING")
    assert result["resource_refs"]["load_balancer_objects_pending"] == "2"
    assert not any(call[0] == "reclaim" for call in cluster.calls)


async def test_disk_that_is_not_deleted_is_unknown(tmp_path, db):  # noqa: F811
    cluster = FakeCluster()
    cluster.csi_deletes = False
    request = executed(tmp_path, db) | {"delete_data": True}
    result = await teardown(db, tmp_path, FakeRuntime(cluster), request)
    assert (result["status"], result["code"]) == ("UNKNOWN", "VOLUME_DELETE_PENDING")


async def test_retry_after_partial_teardown_is_idempotent(tmp_path, db):  # noqa: F811
    cluster = FakeCluster()
    cluster.lb_sticky = True
    request = executed(tmp_path, db)
    first = await teardown(db, tmp_path, FakeRuntime(cluster), request)
    assert first["code"] == "LOAD_BALANCER_DELETE_PENDING"
    cluster.lb_sticky, cluster.lb = False, []  # controller finished deleting the LBs
    second = await teardown(db, tmp_path, FakeRuntime(cluster), request)
    assert (second["status"], second["code"]) == ("SUCCEEDED", "WORKLOADS_REMOVED")


async def test_teardown_runs_as_the_connection_of_the_approved_spec(tmp_path, db):  # noqa: F811
    runtime = FakeRuntime(FakeCluster())
    request = executed(tmp_path, db, credential_ref={"id": "conn-0123456789ab", "version": "1"})
    await teardown(db, tmp_path, runtime, request)
    assert runtime.profiles == ["cmp-conn-0123456789ab"]
    assert runtime.target.cluster_name == "cmp-target"


async def test_other_account_never_reaches_the_cluster(tmp_path, db):  # noqa: F811
    cluster = FakeCluster()
    request = executed(tmp_path, db)
    result = await teardown(db, tmp_path, FakeRuntime(cluster, account="111111111111"), request)
    assert (result["status"], result["code"]) == ("FAILED", "ACCOUNT_MISMATCH")
    assert not cluster.calls


async def test_apply_that_never_made_a_cluster_has_no_workloads(tmp_path, db):  # noqa: F811
    request = executed(tmp_path, db, refs={"state_resource_count": "3"})
    result = await teardown(db, tmp_path, FakeRuntime(FakeCluster()), request)
    assert (result["status"], result["code"]) == ("SUCCEEDED", "NO_CLUSTER")


async def test_running_execution_blocks_teardown(tmp_path, db):  # noqa: F811
    cluster = FakeCluster()
    request = executed(tmp_path, db, status="RUNNING")
    result = await teardown(db, tmp_path, FakeRuntime(cluster), request)
    assert (result["status"], result["code"]) == ("FAILED", "EXECUTION_IN_PROGRESS")
    assert not cluster.calls


@pytest.mark.parametrize("request_", [{"plan_id": "plan-009", "revision": 1},
                                      {"plan_id": "missing", "revision": 1}])  # fmt: skip
async def test_only_executed_infrastructure_revisions_can_be_torn_down(tmp_path, db, request_):  # noqa: F811
    executed(tmp_path, db)
    result = await teardown(db, tmp_path, FakeRuntime(FakeCluster()), request_)
    assert result["status"] == "FAILED"
    assert result["code"] in {"UNSUPPORTED_EXECUTION_SCOPE", "PLAN_NOT_FOUND"}


async def test_cluster_access_failure_is_unknown(tmp_path, db):  # noqa: F811
    request = executed(tmp_path, db)
    result = await teardown(db, tmp_path, FakeRuntime(FakeCluster(), fail=True), request)
    assert (result["status"], result["code"]) == ("UNKNOWN", "CLUSTER_ACCESS_FAILED")


# ---- Workflow (D2, D3, D5, D9) ----


REVIEW = {"digest": "d" * 64, "addresses": ["aws_vpc.target"], "unprotect": False}


def fake_activities(calls, *, workloads=None, planned=None, destroyed=None):
    @activity.defn(name="teardown_cloud_workloads")
    async def workloads_(request):
        calls.append(("workloads", request))
        return workloads or {"status": "SUCCEEDED", "code": "WORKLOADS_REMOVED",
                             "resource_refs": {}}  # fmt: skip

    @activity.defn(name="plan_cloud_destroy")
    async def plan_(request):
        calls.append(("plan", request))
        return planned or {"status": "SUCCEEDED", "code": "DESTROY_PLANNED",
                           "resource_refs": {"delete_count": "1"}, "review": REVIEW}  # fmt: skip

    @activity.defn(name="apply_cloud_destroy")
    async def apply_(request):
        calls.append(("apply", request))
        return destroyed or {"status": "SUCCEEDED", "code": "DESTROYED", "resource_refs": {}}

    return [workloads_, plan_, apply_]


async def run_workflow(calls, confirm=None, wait=None, **kwargs):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[CloudTeardownWorkflow]),
        Worker(env.client, task_queue=PROVISIONING_QUEUE, activities=fake_activities(calls, **kwargs)),
    ):  # fmt: skip
        handle = await env.client.start_workflow(
            CloudTeardownWorkflow.run,
            {"plan_id": "plan-aws", "revision": 1, "delete_data": False},
            id=f"cloud-teardown-{uuid4()}",
            task_queue=WORKFLOW_QUEUE,
        )
        if confirm is not None:
            while (await handle.query(CloudTeardownWorkflow.status))["phase"] != (
                "AWAITING_CONFIRMATION"
            ):
                await env.sleep(1)
            status = await handle.query(CloudTeardownWorkflow.status)
            assert status["review"] == REVIEW
            for digest in confirm:
                await handle.signal(CloudTeardownWorkflow.confirm_destroy, digest)
        if wait is not None:
            await env.sleep(wait)
        result = await handle.result()
        return result, await handle.query(CloudTeardownWorkflow.status)


async def test_workflow_destroys_only_after_the_review_digest_is_confirmed():
    calls = []
    result, status = await run_workflow(calls, confirm=["0" * 64, "d" * 64])
    assert result["code"] == "DESTROYED"
    assert [name for name, _ in calls] == ["workloads", "plan", "apply"]
    assert calls[2][1]["review_digest"] == "d" * 64  # the wrong Digest was ignored
    assert status["phase"] == "FINISHED"


async def test_unconfirmed_review_never_destroys():
    calls = []
    result, _ = await run_workflow(calls, wait=timedelta(hours=25))
    assert (result["status"], result["code"]) == ("UNKNOWN", "DESTROY_NOT_CONFIRMED")
    assert [name for name, _ in calls] == ["workloads", "plan"]


async def test_pending_load_balancers_stop_before_terraform():
    calls = []
    pending = {"status": "UNKNOWN", "code": "LOAD_BALANCER_DELETE_PENDING", "resource_refs": {}}
    result, _ = await run_workflow(calls, workloads=pending)
    assert result["code"] == "LOAD_BALANCER_DELETE_PENDING"
    assert [name for name, _ in calls] == ["workloads"]


async def test_nothing_to_destroy_finishes_without_confirmation():
    calls = []
    planned = {"status": "SUCCEEDED", "code": "NOTHING_TO_DESTROY", "resource_refs": {}}
    result, _ = await run_workflow(calls, planned=planned)
    assert result["code"] == "NOTHING_TO_DESTROY"
    assert [name for name, _ in calls] == ["workloads", "plan"]


# ---- destroy Activities read the executed Revision (D5) ----


async def test_plan_activity_reviews_the_executed_revision(tmp_path, db):  # noqa: F811
    from test_cloud_destroy import AWS_ADDRESSES, DestroyRunner, destroy_plan

    request = executed(tmp_path, db)
    runner = DestroyRunner(plans={"destroy_sequence": [destroy_plan(AWS_ADDRESSES)]})
    activities = CloudTeardownActivities(
        db, artifact_root=tmp_path, aws_profile="cmp-aws-prod", runner=runner
    )
    result = await ActivityEnvironment().run(activities.plan_cloud_destroy, request)
    assert (result["status"], result["code"]) == ("SUCCEEDED", "DESTROY_PLANNED")
    init = next(c for c in runner.commands if c[1:2] == ["init"])
    assert "-backend-config=key=cmp/production-001/terraform.tfstate" in init
    applied = await ActivityEnvironment().run(
        activities.apply_cloud_destroy, request | {"review_digest": result["review"]["digest"]}
    )
    assert applied["code"] == "DESTROYED"
