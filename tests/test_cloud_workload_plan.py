"""Ticket 22 seam: AWS/GCP infrastructure result → WORKLOADS Plan/Preview → approval → the real
09/11 workload Activity → ProvisioningResult → Data/Traffic handoff.

SQLite business DB, local ArtifactStore, real Helm packaging/render, 05/06 fake Terraform tools,
the real 09/11 Activities with their fake cluster/runtime and the real infra-refs resolver.
Expected outcomes come from the ticket 22 decisions (W1~W10).
"""

import itertools
import json

import pytest
import test_plan_publish
from cmp_api.approvals import (
    PlanExecution,
    PlanRevision,
    authenticate_approver,
    claim_execution,
    complete_execution,
    record_decision,
    register_approver,
)
from cmp_api.plan_inputs import input_digest
from cmp_contracts.plan import PlanManifest, Preview
from cmp_contracts.plan_publish import (
    PlanningInputRegistration,
    plan_id_for,
    workload_plan_id_for,
)
from cmp_providers.plans import approval_issues
from cmp_worker.activities import aws_workloads, gcp_workloads
from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.activities.plan_publish import PlanPublishActivities
from temporalio.testing import ActivityEnvironment
from test_aws_workloads import FakeCluster as AwsCluster
from test_aws_workloads import FakeRuntime as AwsRuntime
from test_gcp_workloads import FakeCluster as GcpCluster
from test_gcp_workloads import FakeRuntime as GcpRuntime
from test_plan_publish import (
    DB_IMAGE,
    MIGRATION,
    approver,
    aws_tools,
    gcp_tools,
    publisher,
    register,
    registration,
    revisions,
    stored,
)

# Ticket 18 fixtures (SQLite business DB, local ArtifactStore, API client).
db, store, client = test_plan_publish.db, test_plan_publish.store, test_plan_publish.client

WORKLOADS = {"namespace": "cmp-prod-demo", "storage_class": "cmp-db", "database_image": DB_IMAGE}
AWS_REFS = {
    "vpc_id": "vpc-0123456789abcdef0",
    "eks_cluster_name": "cmp-demo",
    "eks_cluster_arn": "arn:aws:eks:ap-northeast-2:123456789012:cluster/cmp-demo",
    "lb_controller_role_arn": "arn:aws:iam::123456789012:role/cmp-demo-lbc",
}
GCP_REFS = {
    "gcp_project_id": "sbh26-test",
    "gke_cluster_name": "cmp-demo",
    "gke_location": "asia-northeast3",
    "gke_cluster_status": "RUNNING",
}
_APPROVERS = itertools.count()
INFRA_STEP = {"AWS": "execute_aws_plan", "GCP": "execute_gcp_plan"}


def cloud_registration(provider, workloads=WORKLOADS, **settings_override):
    _, body = registration(provider)
    body["target"]["settings"].update(settings_override)
    if workloads is not None:
        body["target"]["workloads"] = workloads
    return PlanningInputRegistration.model_validate(body)


def cloud_publisher(db, store, tmp_path, monkeypatch, provider):
    if provider == "AWS":
        terraform, _ = aws_tools(tmp_path, monkeypatch)
        return publisher(db, store, tmp_path, aws_profile="fixture", terraform_bin=str(terraform))
    template, terraform, _ = gcp_tools(tmp_path, monkeypatch)
    return publisher(
        db,
        store,
        tmp_path,
        aws_profile="cmp-aws-prod",
        gcloud_configuration="cmp-gcp-prod",
        gcp_adc_email="planner@example.com",
        terraform_bin=str(terraform),
        gcp_template_dir=template,
    )


def approve(db, published, approver_id="alice"):
    with db() as session:
        credential = register_approver(
            session, approver_id=f"{approver_id}-{next(_APPROVERS)}", migration_ids=[MIGRATION]
        )
        record, _ = record_decision(
            session,
            approver=authenticate_approver(session, credential),
            plan_id=published["plan_id"],
            revision=published["revision"],
            decision="APPROVE",
            scope=published["scope"],
            manifest_digest=published["manifest_digest"],
            preview_digest=published["preview_digest"],
        )
        claim = claim_execution(session, record.id)
    assert claim["status"] == "CLAIMED", claim
    return claim


def finish(db, claim, *, status="SUCCEEDED", code="APPLY_SUCCEEDED", refs=None, step=None):
    with db() as session:
        complete_execution(
            session,
            claim["approval_id"],
            status=status,
            code=code,
            resource_refs=refs or {},
            step=step,
        )


def infra_ready(db, store, tmp_path, monkeypatch, provider, refs=None, **settings_override):
    register(db, store, cloud_registration(provider, **settings_override))
    pub = cloud_publisher(db, store, tmp_path, monkeypatch, provider)
    infra = pub.publish(MIGRATION)
    assert infra["status"] == "PUBLISHED" and infra["scope"] == "INFRASTRUCTURE", infra
    claim = approve(db, infra)
    default = AWS_REFS if provider == "AWS" else GCP_REFS
    finish(db, claim, refs=refs or default, step=INFRA_STEP[provider])
    return pub, {"plan_id": infra["plan_id"], "revision": infra["revision"]}


def workload_activity(db, tmp_path, provider):
    if provider == "AWS":
        cluster = AwsCluster()
        runtime = AwsRuntime(cluster, account="123456789012")
        activities = aws_workloads.AwsWorkloadsActivities(
            db,
            artifact_root=tmp_path / "plans",
            resolver=aws_workloads.ExecutionRefsResolver(),  # real W11 infra-refs lookup
            runtime=runtime,
            timeouts=aws_workloads.Timeouts(readiness_seconds=0, alb_seconds=0, interval_seconds=0),
        )
        return cluster, activities.execute_aws_workloads
    cluster = GcpCluster()
    runtime = GcpRuntime(cluster)
    runtime.project, runtime.aws_account = "sbh26-test", "123456789012"
    activities = gcp_workloads.GcpWorkloadsActivities(
        db,
        artifact_root=tmp_path / "plans",
        resolver=gcp_workloads.ExecutionRefsResolver(),  # real K12 infra-refs lookup
        runtime=runtime,
        timeouts=gcp_workloads.Timeouts(readiness_seconds=0, ingress_seconds=0, interval_seconds=0),
    )
    return cluster, activities.execute_gcp_workloads


def handoff(db, published, consumer):
    request = {"plan_id": published["plan_id"], "revision": published["revision"]}
    return ApprovalActivities(db)._handoff({**request, "consumer": consumer})


# --- infra result → workload Plan → 09/11 → handoff ------------------------------------------


@pytest.mark.parametrize("provider", ["AWS", "GCP"])
async def test_infra_result_publishes_workload_plan_that_09_11_deploy_and_hand_off(
    db, store, tmp_path, monkeypatch, provider
):
    pub, infra = infra_ready(db, store, tmp_path, monkeypatch, provider)
    published = pub.publish_workloads(MIGRATION, infra)
    assert published["status"] == "PUBLISHED", published
    assert (published["provider"], published["scope"]) == (provider, "WORKLOADS")
    assert published["plan_id"] == workload_plan_id_for(MIGRATION) != plan_id_for(MIGRATION)
    manifest, preview = stored(db, published)
    infra_manifest, _ = stored(db, {**infra})
    assert manifest.target == infra_manifest.target  # 09/11 resolver finds this infra run
    assert approval_issues(manifest, preview) == []
    kinds = {(c.resource_type, c.action) for c in preview.changes}
    assert {("DATABASE", "CREATE"), ("APP", "CREATE"), ("INGRESS", "CREATE")} <= kinds
    assert ("STORAGE", "READ") in kinds

    claim = approve(db, published)
    cluster, execute = workload_activity(db, tmp_path, provider)
    result = await ActivityEnvironment().run(execute, {**claim, "resource_refs": {}})
    assert (result["status"], result["code"]) == ("SUCCEEDED", "WORKLOADS_READY"), result
    assert {r for _, r, _ in cluster.helm_calls} == {"postgres-main", "api-main"}
    assert {ns for ns, _, _ in cluster.helm_calls} == {"cmp-prod-demo"}
    finish(db, claim, code=result["code"], refs=result["resource_refs"], step=execute.__name__)

    data = handoff(db, published, "DATA_MIGRATION")
    traffic = handoff(db, published, "TRAFFIC_MIGRATION")
    assert data["proceed"] is True and data["database"]["ready"] is True, data
    assert traffic["proceed"] is True and traffic["application"]["external_endpoint"], traffic
    # W10 + no Secret values anywhere the platform stores or returns.
    password = cluster.secrets[("cmp-prod-demo", "postgres-main-auth")]["password"]
    with db() as session:
        rows = [r.manifest for r in session.query(PlanRevision)] + [
            r.preview for r in session.query(PlanRevision)
        ]
        rows += [e.resource_refs for e in session.query(PlanExecution)]
    blobs = json.dumps([rows, published, result, data, traffic])
    assert password not in blobs
    folder = tmp_path / "plans" / f"{published['plan_id']}-r{published['revision']}"
    assert all(password.encode() not in path.read_bytes() for path in folder.iterdir())


async def test_activity_publishes_workloads_and_scaffold_without_publisher(
    db, store, tmp_path, monkeypatch
):
    pub, infra = infra_ready(db, store, tmp_path, monkeypatch, "AWS")
    request = {"migration_id": MIGRATION, "infrastructure": infra}
    env = ActivityEnvironment()
    result = await env.run(PlanPublishActivities(pub).publish_workload_plan, request)
    assert (result["status"], result["scope"]) == ("PUBLISHED", "WORKLOADS")
    none = await env.run(PlanPublishActivities(None).publish_workload_plan, request)
    assert none == {"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"}


# --- W5 re-run ---------------------------------------------------------------------------------


def test_same_infra_result_reuses_and_new_infra_revision_is_new(db, store, tmp_path, monkeypatch):
    pub, infra = infra_ready(db, store, tmp_path, monkeypatch, "AWS")
    first = pub.publish_workloads(MIGRATION, infra)
    count = revisions(db)
    again = pub.publish_workloads(MIGRATION, infra)
    assert again["code"] == "PLAN_REUSED" and again["revision"] == first["revision"]
    assert revisions(db) == count

    # A changed infrastructure input → new infra Revision → its run gives new refs.
    register(db, store, cloud_registration("AWS", node_count=3))
    infra2 = pub.publish(MIGRATION)
    assert infra2["revision"] == 2
    claim = approve(db, infra2, "bob")
    finish(db, claim, refs={**AWS_REFS, "eks_cluster_arn": AWS_REFS["eks_cluster_arn"] + "-2"})
    second = pub.publish_workloads(
        MIGRATION, {"plan_id": infra2["plan_id"], "revision": infra2["revision"]}
    )
    assert (second["status"], second["code"], second["revision"]) == (
        "PUBLISHED",
        "PLAN_PUBLISHED",
        first["revision"] + 1,
    )


def test_workload_settings_do_not_change_the_infrastructure_revision(
    db, store, tmp_path, monkeypatch
):
    register(db, store, cloud_registration("AWS", workloads=None))
    pub = cloud_publisher(db, store, tmp_path, monkeypatch, "AWS")
    first = pub.publish(MIGRATION)
    register(db, store, cloud_registration("AWS"))
    assert pub.publish(MIGRATION) == {**first, "code": "PLAN_REUSED"}
    # Existing registrations keep their 18 input Digest (no `workloads` key in it).
    without = cloud_registration("AWS", workloads=None)
    assert input_digest(without) == input_digest(cloud_registration("AWS"))


# --- W3/W4 blocked publication -------------------------------------------------------------------


def test_workload_plan_needs_succeeded_latest_infrastructure(db, store, tmp_path, monkeypatch):
    register(db, store, cloud_registration("AWS"))
    pub = cloud_publisher(db, store, tmp_path, monkeypatch, "AWS")
    infra = pub.publish(MIGRATION)
    ref = {"plan_id": infra["plan_id"], "revision": infra["revision"]}

    def code(target=ref):
        result = pub.publish_workloads(MIGRATION, target)
        assert result["status"] == "FAILED", result
        return result["code"]

    assert code() == "INFRASTRUCTURE_NOT_SUCCEEDED"  # not approved/executed yet
    assert code({**ref, "revision": 9}) == "INFRASTRUCTURE_NOT_FOUND"
    assert code({"plan_id": workload_plan_id_for(MIGRATION), "revision": 1}) == (
        "INFRASTRUCTURE_NOT_FOUND"
    )
    claim = approve(db, infra)
    finish(db, claim, status="FAILED", code="TERRAFORM_APPLY_FAILED")
    assert code() == "INFRASTRUCTURE_NOT_SUCCEEDED"
    before = revisions(db)

    register(db, store, cloud_registration("AWS", node_count=3))
    infra2 = pub.publish(MIGRATION)
    assert code() == "INFRASTRUCTURE_STALE"  # revision 1 is no longer the latest
    claim = approve(db, infra2, "bob")
    finish(db, claim, refs={"vpc_id": "vpc-1"})  # no EKS cluster refs
    assert code({**ref, "revision": 2}) == "INFRASTRUCTURE_OUTPUTS_MISSING"
    assert revisions(db) == before + 1  # only infra2; no workload Revision published


def test_missing_workload_settings_or_onprem_is_not_published(db, store, tmp_path, monkeypatch):
    pub, infra = infra_ready(db, store, tmp_path, monkeypatch, "AWS", workloads=None)
    failed = pub.publish_workloads(MIGRATION, infra)
    assert (failed["status"], failed["code"]) == ("FAILED", "WORKLOAD_SETTINGS_MISSING")
    missing = pub.publish_workloads("migration-unknown", infra)
    assert missing["code"] == "PLANNING_INPUT_MISSING"
    register(db, store, registration()[0])  # the same Migration re-registered as ON_PREM
    onprem = pub.publish_workloads(MIGRATION, infra)
    assert (onprem["status"], onprem["code"]) == ("FAILED", "WORKLOADS_NOT_APPLICABLE")


# --- W10 Plan view ---------------------------------------------------------------------------


def test_migration_plan_view_returns_the_workload_plan_once_published(
    db, store, tmp_path, monkeypatch, client
):
    pub, infra = infra_ready(db, store, tmp_path, monkeypatch, "AWS")
    headers = approver(client, "carol")
    view = client.get(f"/provisioning/migrations/{MIGRATION}/plan", headers=headers).json()
    assert (view["plan_id"], view["scope"]) == (infra["plan_id"], "INFRASTRUCTURE")
    published = pub.publish_workloads(MIGRATION, infra)
    view = client.get(f"/provisioning/migrations/{MIGRATION}/plan", headers=headers).json()
    assert (view["plan_id"], view["scope"], view["approvable"]) == (
        published["plan_id"],
        "WORKLOADS",
        True,
    )
    assert view["preview_digest"] == published["preview_digest"]
    manifest = PlanManifest.model_validate(stored(db, published)[0].model_dump())
    assert Preview.model_validate(view["preview"]).manifest_digest == view["manifest_digest"]
    assert manifest.image_uri and manifest.scope == "WORKLOADS"
