"""Ticket 13 seam: approval Workflow completion -> business DB ProvisioningResult -> handoff/API.

Provider refs below are the real result shapes recorded by tickets 09 (AWS), 11 (GCP) and
12 (K3s), plus the standard keys of decision R4. Expected values come from decisions R1~R10,
not from the implementation.
"""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from cmp_api.approval_delivery import TemporalApprovalDelivery
from cmp_api.approvals import (
    authenticate_approver,
    complete_execution,
    publish_plan_revision,
    record_decision,
    register_approver,
)
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.provisioning_results import load_result
from cmp_api.routes.approvals import get_approval_delivery, get_session
from cmp_contracts.plan import PlanManifest, Preview
from cmp_contracts.queues import (
    APPROVAL_WORKFLOW_NAME,
    PROVISIONING_QUEUE,
    WORKFLOW_QUEUE,
    approval_workflow_id,
)
from cmp_providers.artifacts import sha256_json
from cmp_providers.plans import manifest_digest
from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.workflows.approval import ProvisioningApprovalWorkflow
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

FIXTURES = Path(__file__).parent / "fixtures/contracts"
DB_PASSWORD = "generated-db-password-must-never-appear"

# Ticket 11 real GKE run + R4 standard keys.
GCP_WORKLOAD_REFS = {
    "gcp_project_id": "sbh26-prod",
    "gke_cluster_name": "cmp-target-gke",
    "gke_location": "asia-northeast3",
    "storage_class": "cmp-pd-balanced",
    "namespace": "cmp-demo-target",
    "image_pull_secret": "cmp-ecr-pull",
    "db_secret_ref": "cmp-demo-db-auth/password",
    "db_release": "cmp-demo-db",
    "db_pvc": "cmp-demo-db",
    "db_host": "cmp-demo-db.cmp-demo-target.svc",
    "db_port": "5432",
    "db_name": "demoapp",
    "db_user": "demoapp",
    "db_replication_capable": "true",
    "db_version": "16",
    "db_storage_size_gib": "10",
    "app_release": "cmp-demo-api",
    "app_image": "714066696240.dkr.ecr.ap-northeast-2.amazonaws.com/cmp-demo-api@sha256:"
    + "b" * 64,
    "app_internal_endpoint": "http://cmp-demo-api.cmp-demo-target.svc:8080",
    "app_port": "8080",
    "app_health_path": "/health",
    "app_replicas": "1",
    "ingress": "cmp-demo-target/cmp-demo-api",
    "ingress_ip": "34.102.136.6",
    "app_external_endpoint": "http://34.102.136.6",
}
GCP_INFRA_REFS = {
    "gcp_project_id": "sbh26-prod",
    "vpc_network": "cmp-target-vpc",
    "subnetwork": "cmp-target-subnet",
    "gke_cluster_name": "cmp-target-gke",
    "gke_location": "asia-northeast3",
    "node_pool_name": "cmp-target-gke-primary",
    "gke_cluster_status": "RUNNING",
    "node_pool_status": "RUNNING",
}
# Ticket 09 real EKS run + R4 standard keys; FULL also carries ticket 08 infra refs.
AWS_REFS = {
    "eks_cluster_name": "cmp-target-aws",
    "vpc_id": "vpc-0123456789abcdef0",
    "cluster_name": "cmp-target-aws",
    "lb_controller": "kube-system/aws-load-balancer-controller",
    "storage_class": "cmp-gp3",
    "namespace": "cmp-demo-target",
    "db_secret_ref": "cmp-demo-db-auth/DB_PASSWORD",
    "db_release": "cmp-demo-db",
    "db_pvc": "cmp-demo-db",
    "db_host": "cmp-demo-db.cmp-demo-target.svc",
    "db_port": "5432",
    "db_name": "demoapp",
    "db_user": "demoapp",
    "db_replication_capable": "true",
    "db_version": "16",
    "db_storage_size_gib": "10",
    "app_release": "cmp-demo-api",
    "app_image": "714066696240.dkr.ecr.ap-northeast-2.amazonaws.com/cmp-demo-api@sha256:"
    + "b" * 64,
    "app_internal_endpoint": "http://cmp-demo-api.cmp-demo-target.svc:8080",
    "app_port": "8080",
    "app_health_path": "/health",
    "app_replicas": "1",
    "ingress": "cmp-demo-target/cmp-demo-api",
    "alb_hostname": "k8s-cmpdemot-cmpdemoa-1.ap-northeast-2.elb.amazonaws.com",
    "app_external_endpoint": "http://k8s-cmpdemot-cmpdemoa-1.ap-northeast-2.elb.amazonaws.com",
}
# Ticket 12 real K3s refs (legacy keys kept) + R4 standard keys.
ONPREM_REFS = {
    "job_id": "rollout-23d0a1fc6da16776771b2107",
    "namespace": "demo",
    "database_release": "demo-db",
    "application_release": "demo-app",
    "database_endpoint": "demo-db.demo.svc:5432",
    "application_endpoint": "demo-app.demo.svc:8080",
    "database_secret": "demo-db-auth/password",
    "agent_id": "agent-001",
    "cluster_uid": "k3s-uid-001",
    "storage_class": "local-path",
    "db_host": "demo-db.demo.svc",
    "db_port": "5432",
    "db_name": "demo",
    "db_user": "app_user",
    "db_version": "16",
    "db_storage_size_gib": "5",
    "db_secret_ref": "demo-db-auth/password",
    "db_release": "demo-db",
    "app_release": "demo-app",
    "app_port": "8080",
    "app_health_path": "/health",
    "app_replicas": "2",
    "app_internal_endpoint": "http://demo-app.demo.svc:8080",
}


def plan_manifest(provider: str, scope: str, plan_id: str = "plan-001") -> PlanManifest:
    name = {"AWS": "aws", "GCP": "gcp", "ON_PREM": "on_prem"}[provider]
    data = json.loads((FIXTURES / f"{name}-plan.json").read_text())
    data["scope"] = scope
    data["plan"]["plan_id"] = plan_id
    if scope == "WORKLOADS":
        data["artifacts"].pop("terraform_plan", None)
    return PlanManifest.model_validate(data)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


class Approved:
    """Publishes and approves one Revision; keeps the approver credential for API calls."""

    def __init__(self, db, provider="GCP", scope="WORKLOADS", plan_id="plan-001"):
        self.manifest = plan_manifest(provider, scope, plan_id)
        preview = Preview(
            contract_version="1",
            manifest_digest=manifest_digest(self.manifest),
            changes=[{"resource_type": "APP", "action": "CREATE", "name": "demo"}],
        )
        with db() as session:
            publish_plan_revision(session, self.manifest, preview)
            self.credential = register_approver(
                session,
                approver_id=f"alice-{plan_id}",
                migration_ids=[self.manifest.migration_id],
            )
            record, _ = record_decision(
                session,
                approver=authenticate_approver(session, self.credential),
                plan_id=plan_id,
                revision=1,
                decision="APPROVE",
                scope=scope,
                manifest_digest=manifest_digest(self.manifest),
                preview_digest=sha256_json(preview.model_dump(mode="json")),
            )
            self.approval_id = record.id
        self.plan_id = plan_id


class Providers:
    """Fake provider Activities returning recorded real result shapes."""

    def __init__(self):
        self.outcomes: dict[str, dict] = {}
        self.lost: set[str] = set()

    def activities(self):
        def make(name):
            @activity.defn(name=name)
            async def run(request: dict) -> dict:
                if name in self.lost:
                    raise RuntimeError("worker lost")
                return self.outcomes[name]

            return run

        names = [
            "execute_aws_plan",
            "execute_aws_workloads",
            "execute_gcp_plan",
            "execute_gcp_workloads",
            "execute_on_prem_plan",
        ]
        return [make(name) for name in names]


@pytest.fixture
def providers():
    return Providers()


@pytest.fixture
async def temporal(db, providers):
    approval = ApprovalActivities(db)
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ProvisioningApprovalWorkflow]),
        Worker(
            env.client,
            task_queue=PROVISIONING_QUEUE,
            activities=[
                approval.claim_approved_execution,
                approval.complete_approved_execution,
                *providers.activities(),
            ],
        ),
    ):
        yield env.client


async def execute(client, approved: Approved) -> dict:
    handle = await client.start_workflow(
        APPROVAL_WORKFLOW_NAME,
        {"plan_id": approved.plan_id, "revision": 1},
        id=approval_workflow_id(approved.plan_id, 1),
        task_queue=WORKFLOW_QUEUE,
    )
    await TemporalApprovalDelivery(client).deliver(
        approval_id=approved.approval_id, plan_id=approved.plan_id, revision=1
    )
    return await asyncio.wait_for(handle.result(), timeout=30)


def stored(db, plan_id="plan-001"):
    with db() as session:
        return load_result(session, plan_id, 1)


def checks(result) -> dict[str, str]:
    return {item.name: item.status for item in result.verification}


async def handoff(db, consumer, plan_id="plan-001") -> dict:
    activities = ApprovalActivities(db)
    return await ActivityEnvironment().run(
        activities.get_migration_handoff,
        {"plan_id": plan_id, "revision": 1, "consumer": consumer},
    )


# --- READY paths per provider -------------------------------------------------------------


async def test_gcp_ready_result_is_normalized_and_both_handoffs_proceed(temporal, db, providers):
    approved = Approved(db, "GCP", "WORKLOADS")
    providers.outcomes["execute_gcp_workloads"] = {
        "status": "SUCCEEDED",
        "code": "WORKLOADS_READY",
        "resource_refs": GCP_WORKLOAD_REFS,
    }
    outcome = await execute(temporal, approved)

    assert outcome["status"] == "EXECUTED"
    assert set(outcome) == {"approval_id", "plan_id", "revision", "status", "code"}
    result = stored(db)
    assert (result.status, result.code, result.data_state) == (
        "READY",
        "WORKLOADS_READY",
        "NOT_MIGRATED",
    )
    assert (result.provider, result.scope, result.approval_id) == (
        "GCP",
        "WORKLOADS",
        approved.approval_id,
    )
    assert result.migration_id == approved.manifest.migration_id
    assert (result.cluster.kind, result.cluster.name, result.cluster.location) == (
        "GKE",
        "cmp-target-gke",
        "asia-northeast3",
    )
    assert result.namespace == "cmp-demo-target"

    db_part = result.database
    assert (db_part.host, db_part.port, db_part.name, db_part.user, db_part.version) == (
        "cmp-demo-db.cmp-demo-target.svc",
        5432,
        "demoapp",
        "demoapp",
        "16",
    )
    assert db_part.password_secret.model_dump() == {
        "namespace": "cmp-demo-target",
        "name": "cmp-demo-db-auth",
        "key": "password",
    }
    assert db_part.replication_capable is True
    assert db_part.exposure == "CLUSTER_INTERNAL"
    assert (db_part.storage.pvc, db_part.storage.storage_class, db_part.storage.size_gib) == (
        "cmp-demo-db",
        "cmp-pd-balanced",
        10,
    )
    assert db_part.ready is True

    app_part = result.application
    assert app_part.external_endpoint == "http://34.102.136.6"
    assert app_part.internal_endpoint == "http://cmp-demo-api.cmp-demo-target.svc:8080"
    assert (app_part.ingress.kind, app_part.ingress.address) == ("GKE_INGRESS", "34.102.136.6")
    assert (app_part.port, app_part.health_path, app_part.scheme, app_part.tls) == (
        8080,
        "/health",
        "http",
        False,
    )
    assert (app_part.replicas, app_part.ready) == (1, True)

    statuses = checks(result)
    assert statuses.pop("functional_tests") == "NOT_RUN"
    assert set(statuses.values()) == {"PASSED"}
    assert {"pvc_bound", "db_auth_connect", "external_health"} <= set(statuses)
    assert result.failure is None

    data = await handoff(db, "DATA_MIGRATION")
    assert data["proceed"] is True and data["blocked_by"] == []
    assert data["database"]["host"] == "cmp-demo-db.cmp-demo-target.svc"
    assert "application" not in data
    traffic = await handoff(db, "TRAFFIC_MIGRATION")
    assert traffic["proceed"] is True
    assert traffic["application"]["external_endpoint"] == "http://34.102.136.6"
    assert "database" not in traffic


async def test_aws_full_result_merges_infra_and_workload_refs(temporal, db, providers):
    approved = Approved(db, "AWS", "FULL")
    providers.outcomes["execute_aws_plan"] = {
        "status": "SUCCEEDED",
        "code": "TERRAFORM_APPLIED",
        "resource_refs": {"eks_cluster_name": "cmp-target-aws", "vpc_id": AWS_REFS["vpc_id"]},
    }
    providers.outcomes["execute_aws_workloads"] = {
        "status": "SUCCEEDED",
        "code": "WORKLOADS_READY",
        "resource_refs": {k: v for k, v in AWS_REFS.items() if k not in {"vpc_id"}},
    }
    await execute(temporal, approved)

    result = stored(db)
    assert result.status == "READY"
    assert (result.cluster.kind, result.cluster.name) == ("EKS", "cmp-target-aws")
    assert result.cluster.location == approved.manifest.target.region
    assert result.cluster.network_refs == {"vpc_id": AWS_REFS["vpc_id"]}
    assert result.database.password_secret.key == "DB_PASSWORD"
    assert result.application.ingress.kind == "ALB"
    assert result.application.ingress.address == AWS_REFS["alb_hostname"]
    assert result.application.external_endpoint == AWS_REFS["app_external_endpoint"]


async def test_onprem_ready_result_carries_agent_location_and_blocks_traffic(
    temporal, db, providers
):
    approved = Approved(db, "ON_PREM", "FULL")
    providers.outcomes["execute_on_prem_plan"] = {
        "status": "SUCCEEDED",
        "code": "READY",
        "resource_refs": ONPREM_REFS,
    }
    await execute(temporal, approved)

    result = stored(db)
    assert result.status == "READY"
    assert result.cluster.kind == "K3s"
    assert (result.cluster.agent_id, result.cluster.cluster_uid) == ("agent-001", "k3s-uid-001")
    assert (result.database.host, result.database.name, result.database.user) == (
        "demo-db.demo.svc",
        "demo",
        "app_user",
    )
    assert result.database.replication_capable is None
    assert result.application.external_endpoint is None
    assert {"REPLICATION_NOT_VERIFIED", "EXTERNAL_ENDPOINT_UNAVAILABLE"} <= set(result.warnings)
    assert checks(result)["external_health"] == "NOT_RUN"

    data = await handoff(db, "DATA_MIGRATION")
    # Ticket 16 T2: on-prem data moves through the tunnel; blocked until it is verified.
    assert data["proceed"] is False and data["blocked_by"] == ["TUNNEL_DB_NOT_VERIFIED"]
    assert data["common"]["cluster"]["agent_id"] == "agent-001"
    traffic = await handoff(db, "TRAFFIC_MIGRATION")
    assert traffic["proceed"] is False
    assert traffic["blocked_by"] == ["EXTERNAL_ENDPOINT_UNAVAILABLE"]


# --- failures block the next stage ----------------------------------------------------------


async def test_post_deploy_check_failure_records_stage_partial_refs_and_blocks(
    temporal, db, providers
):
    approved = Approved(db, "GCP", "WORKLOADS")
    partial = {
        k: GCP_WORKLOAD_REFS[k]
        for k in (
            "gcp_project_id",
            "gke_cluster_name",
            "gke_location",
            "storage_class",
            "namespace",
            "db_secret_ref",
            "db_release",
        )
    }
    providers.outcomes["execute_gcp_workloads"] = {
        "status": "FAILED",
        "code": "PVC_NOT_BOUND",
        "resource_refs": partial,
    }
    outcome = await execute(temporal, approved)

    assert outcome["status"] == "FAILED"
    result = stored(db)
    assert (result.status, result.code) == ("FAILED", "PVC_NOT_BOUND")
    assert result.failure.model_dump() == {
        "stage": "POST_DEPLOY_VERIFICATION",
        "code": "PVC_NOT_BOUND",
        "recoverability": "RETRY_AFTER_FIX",
    }
    statuses = checks(result)
    assert statuses["storage_class"] == "PASSED"
    assert statuses["pvc_bound"] == "FAILED"
    assert statuses["db_auth_connect"] == "NOT_RUN"
    assert statuses["external_health"] == "NOT_RUN"
    assert result.database.host is None and result.database.ready is False
    assert result.application.external_endpoint is None and result.application.ready is False

    for consumer in ("DATA_MIGRATION", "TRAFFIC_MIGRATION"):
        view = await handoff(db, consumer)
        assert view["proceed"] is False
        assert "PROVISIONING_NOT_READY" in view["blocked_by"]


async def test_lost_infra_step_is_unknown_infrastructure_failure(temporal, db, providers):
    approved = Approved(db, "GCP", "FULL")
    providers.lost.add("execute_gcp_plan")
    outcome = await execute(temporal, approved)

    assert outcome["status"] == "UNKNOWN"
    result = stored(db)
    assert result.status == "UNKNOWN"
    assert result.failure.stage == "INFRASTRUCTURE"
    assert result.failure.recoverability == "CHECK_STATE_FIRST"
    assert result.database is None and result.application is None


async def test_precondition_failure_runs_no_checks(temporal, db, providers):
    approved = Approved(db, "GCP", "WORKLOADS")
    providers.outcomes["execute_gcp_workloads"] = {
        "status": "FAILED",
        "code": "ARTIFACT_DIGEST_MISMATCH",
        "resource_refs": {},
    }
    await execute(temporal, approved)

    result = stored(db)
    assert result.failure.stage == "PRECONDITION"
    assert set(checks(result).values()) == {"NOT_RUN"}


async def test_infrastructure_scope_success_is_not_ready_for_migration(temporal, db, providers):
    approved = Approved(db, "GCP", "INFRASTRUCTURE")
    providers.outcomes["execute_gcp_plan"] = {
        "status": "SUCCEEDED",
        "code": "TERRAFORM_APPLIED",
        "resource_refs": GCP_INFRA_REFS,
    }
    await execute(temporal, approved)

    result = stored(db)
    assert result.status == "INFRASTRUCTURE_READY"
    assert result.cluster.network_refs == {
        "vpc_network": "cmp-target-vpc",
        "subnetwork": "cmp-target-subnet",
    }
    assert result.database is None and result.application is None
    view = await handoff(db, "DATA_MIGRATION")
    assert view["proceed"] is False
    assert view["blocked_by"] == ["WORKLOADS_NOT_DEPLOYED"]


async def test_missing_result_is_reported_not_guessed(db):
    view = await handoff(db, "DATA_MIGRATION", plan_id="plan-none")
    assert view == {
        "plan_id": "plan-none",
        "revision": 1,
        "consumer": "DATA_MIGRATION",
        "proceed": False,
        "blocked_by": ["RESULT_NOT_FOUND"],
    }


# --- secrets, immutability -------------------------------------------------------------------


def test_result_drops_unknown_keys_and_never_contains_secret_values(db):
    approved = Approved(db, "GCP", "WORKLOADS")
    refs = {**GCP_WORKLOAD_REFS, "db_password": DB_PASSWORD, "ecr_token": "token-value"}
    with db() as session:
        from cmp_api.approvals import claim_execution

        claim_execution(session, approved.approval_id)
        complete_execution(
            session,
            approved.approval_id,
            status="SUCCEEDED",
            code="WORKLOADS_READY",
            step="execute_gcp_workloads",
            resource_refs=refs,
        )
    text = stored(db).model_dump_json()
    assert DB_PASSWORD not in text and "token-value" not in text


def test_result_is_immutable_after_first_completion(db):
    approved = Approved(db, "GCP", "WORKLOADS")
    with db() as session:
        from cmp_api.approvals import claim_execution

        claim_execution(session, approved.approval_id)
        for status, code in (("SUCCEEDED", "WORKLOADS_READY"), ("FAILED", "LATE_REPORT")):
            complete_execution(
                session,
                approved.approval_id,
                status=status,
                code=code,
                step="execute_gcp_workloads",
                resource_refs=deepcopy(GCP_WORKLOAD_REFS),
            )
    assert stored(db).status == "READY"


# --- public read-only summary API (R8) -------------------------------------------------------


@pytest.fixture
def client(db):
    def session():
        with db() as s:
            yield s

    class NoDelivery:
        async def workflow_state(self, **_):
            return "UNKNOWN"

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_approval_delivery] = NoDelivery
    yield TestClient(app)
    app.dependency_overrides.clear()


def complete_gcp(db, approved):
    with db() as session:
        from cmp_api.approvals import claim_execution

        claim_execution(session, approved.approval_id)
        complete_execution(
            session,
            approved.approval_id,
            status="SUCCEEDED",
            code="WORKLOADS_READY",
            step="execute_gcp_workloads",
            resource_refs=GCP_WORKLOAD_REFS,
        )


URL = "/provisioning/plans/plan-001/revisions/1/result"


def test_result_api_exposes_only_the_user_summary(db, client):
    approved = Approved(db, "GCP", "WORKLOADS")
    complete_gcp(db, approved)
    response = client.get(URL, headers={"Authorization": f"Bearer {approved.credential}"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY" and body["data_state"] == "NOT_MIGRATED"
    assert body["cluster"] == {
        "kind": "GKE",
        "name": "cmp-target-gke",
        "location": "asia-northeast3",
    }
    assert body["application"] == {
        "external_endpoint": "http://34.102.136.6",
        "health_path": "/health",
        "scheme": "http",
    }
    assert body["failure"] is None
    assert any(item["name"] == "external_health" for item in body["verification"])
    text = response.text
    for hidden in (
        "cmp-demo-db.cmp-demo-target.svc",
        "cmp-demo-db-auth",
        "svc:8080",
        "sbh26-prod",
        "cmp-demo-api",
    ):
        assert hidden not in text
    assert "database" not in body


def test_result_api_requires_authorized_approver(db, client):
    approved = Approved(db, "GCP", "WORKLOADS")
    complete_gcp(db, approved)
    assert client.get(URL).status_code == 401
    with db() as session:
        other = register_approver(session, approver_id="mallory", migration_ids=["other"])
    assert client.get(URL, headers={"Authorization": f"Bearer {other}"}).status_code == 403


def test_result_api_404_before_completion(db, client):
    approved = Approved(db, "GCP", "WORKLOADS")
    response = client.get(URL, headers={"Authorization": f"Bearer {approved.credential}"})
    assert response.status_code == 404
    assert response.json()["detail"] == "RESULT_NOT_FOUND"


def test_odd_ref_values_never_block_execution_completion(db):
    approved = Approved(db, "GCP", "WORKLOADS")
    refs = {**GCP_WORKLOAD_REFS, "db_port": "", "app_health_path": "  ", "db_name": " demoapp "}
    with db() as session:
        from cmp_api.approvals import PlanApproval, claim_execution, execution_status

        claim_execution(session, approved.approval_id)
        complete_execution(
            session,
            approved.approval_id,
            status="SUCCEEDED",
            code="WORKLOADS_READY",
            step="execute_gcp_workloads",
            resource_refs=refs,
        )
        assert execution_status(session, session.get(PlanApproval, approved.approval_id)) == (
            "SUCCEEDED"
        )
    result = stored(db)
    assert result.database.port is None and result.database.name == "demoapp"
    assert result.application.health_path is None
