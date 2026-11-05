"""Ticket 18 seam: Planning input v2 + target settings → published Plan Revision → Preview API
→ approval → claimed execution. SQLite business DB, local ArtifactStore, real Helm packaging,
05/06 fake Terraform tools and a fake Agent (no cloud, no cluster).
"""

import json
from pathlib import Path

import pytest
from cmp_api.agent_registry import revoke_agent
from cmp_api.approvals import PlanRevision, claim_execution
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.onprem_rollout import LocalArtifactStore
from cmp_api.plan_inputs import register_planning_input
from cmp_api.routes.approvals import get_approval_delivery, get_session
from cmp_contracts.plan import PlanManifest, Preview
from cmp_contracts.plan_publish import PlanningInputRegistration
from cmp_providers.plans import approval_issues
from cmp_worker.activities.onprem_execution import OnPremExecutionActivities
from cmp_worker.activities.plan_publish import PlanPublishActivities
from cmp_worker.plan_publish import PlanPublisher, plan_id_for
from fastapi.testclient import TestClient
from onprem_rollout_support import register_agent
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment
from test_approval_gate import FakeDelivery
from test_aws_preview import _fake_tools as aws_tools
from test_aws_preview import request as aws_request
from test_gcp_preview import fake_tools as gcp_tools
from test_gcp_preview import request as gcp_request
from test_provisioning_v2 import _request as planning_request

ADMIN = "admin-key-for-tests"
MIGRATION = "migration-1"
DB_IMAGE = "docker.io/library/postgres@sha256:" + "e" * 64


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(tmp_path / "store")


@pytest.fixture(autouse=True)
def platform_registry(monkeypatch):
    """Ticket 29 (I1): the planning image is in ECR, so ON_PREM jobs need it configured."""
    from cmp_api.config import settings

    monkeypatch.setattr(
        settings, "platform_ecr_registry", "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"
    )
    monkeypatch.setattr(settings, "platform_ecr_repositories", "demo")


def onprem_target(**override):
    return {
        "provider": "ON_PREM",
        "environment_id": "onprem-prod-001",
        "agent_id": "agent-a",
        "cluster_uid": "cluster-a",
        "namespace": "cmp-prod-demo",
        "storage_class": "local-path",
        "database_image": DB_IMAGE,
        **override,
    }


def cloud_settings(model):
    data = model.model_dump(mode="json")
    del data["migration_id"], data["plan"]
    return data


def registration(provider="ON_PREM", target=None, requirements=None, **request_override):
    payload, content = planning_request(requirements)
    payload["target_selection"]["provider"] = provider
    if provider == "AWS":
        payload["target_selection"]["location"] = "ap-northeast-2"
        target = target or {"provider": "AWS", "settings": cloud_settings(aws_request())}
    elif provider == "GCP":
        payload["target_selection"]["location"] = "asia-northeast3"
        target = target or {"provider": "GCP", "settings": cloud_settings(gcp_request())}
    payload.update(request_override)
    body = {
        "contract_version": "1",
        "planning_request": payload,
        "target": target or onprem_target(),
        "requirements_json": content.decode(),
    }
    return PlanningInputRegistration.model_validate(body), body


def register(db, store, reg):
    with db() as session:
        return register_planning_input(session, MIGRATION, reg, store)


def publisher(db, store, tmp_path, **kwargs):
    return PlanPublisher(db, store, cloud_artifact_root=tmp_path / "plans", **kwargs)


def revisions(db):
    with db() as session:
        return session.scalar(select(func.count()).select_from(PlanRevision))


def stored(db, result):
    with db() as session:
        row = session.get(PlanRevision, (result["plan_id"], result["revision"]))
        return PlanManifest.model_validate(row.manifest), Preview.model_validate(row.preview)


@pytest.fixture
def onprem(db, store):
    with db() as session:
        register_agent(session)
    register(db, store, registration()[0])


# --- ON_PREM ---------------------------------------------------------------------------------


def test_onprem_input_publishes_an_approvable_workloads_revision(db, store, tmp_path, onprem):
    result = publisher(db, store, tmp_path).publish(MIGRATION)
    assert result["status"] == "PUBLISHED" and result["code"] == "PLAN_PUBLISHED"
    assert result["plan_id"] == plan_id_for(MIGRATION) and result["revision"] == 1
    assert (result["provider"], result["scope"]) == ("ON_PREM", "WORKLOADS")
    manifest, preview = stored(db, result)
    assert manifest.target.agent_id == "agent-a" and manifest.target.cluster_ref.id == "cluster-a"
    assert manifest.target.namespace == "cmp-prod-demo"
    assert manifest.image_uri.endswith("b" * 64)
    assert manifest.plan.template_version == "helm-v2"
    for ref in manifest.artifacts.values():  # exact bytes in the platform store
        store.read(ref)
    assert approval_issues(manifest, preview) == []
    assert {c.resource_type for c in preview.changes} >= {"DATABASE", "APP", "STORAGE"}
    assert result["manifest_digest"] == preview.manifest_digest


def test_same_input_reuses_revision_and_changed_input_is_new(db, store, tmp_path, onprem):
    first = publisher(db, store, tmp_path).publish(MIGRATION)
    again = publisher(db, store, tmp_path).publish(MIGRATION)
    assert again["code"] == "PLAN_REUSED"
    assert (again["revision"], again["preview_digest"]) == (
        first["revision"],
        first["preview_digest"],
    )
    assert revisions(db) == 1
    register(db, store, registration(target=onprem_target(namespace="cmp-prod-two"))[0])
    changed = publisher(db, store, tmp_path).publish(MIGRATION)
    assert (changed["code"], changed["revision"]) == ("PLAN_PUBLISHED", 2)
    register(db, store, registration()[0])  # back to the first input: still a new Revision
    assert publisher(db, store, tmp_path).publish(MIGRATION)["revision"] == 3


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("missing", "AGENT_NOT_REGISTERED"),
        ("revoked", "AGENT_REVOKED"),
        ("cluster", "CLUSTER_UID_MISMATCH"),
    ],
)
def test_unusable_agent_is_never_published(db, store, tmp_path, setup, code):
    with db() as session:
        if setup != "missing":
            register_agent(session, cluster_uid="other" if setup == "cluster" else "cluster-a")
        if setup == "revoked":
            revoke_agent(session, agent_id="agent-a")
    register(db, store, registration()[0])
    result = publisher(db, store, tmp_path).publish(MIGRATION)
    assert result == {"status": "FAILED", "code": code, "issues": [code]}
    assert revisions(db) == 0


def test_invalid_planning_input_or_topology_is_not_published(db, store, tmp_path):
    with db() as session:
        register_agent(session)
    payload, _ = planning_request()
    sandbox = {**payload["sandbox"], "status": "FAILED"}
    register(db, store, registration(sandbox=sandbox)[0])
    failed = publisher(db, store, tmp_path).publish(MIGRATION)
    assert failed["status"] == "FAILED" and failed["code"] == "PLANNING_INPUT_INVALID"
    assert "PRECONDITION_FAILED" in failed["issues"]

    from test_provisioning_v2 import _requirements

    requirements = _requirements()
    second_db = {**requirements["resources"][0], "id": "postgres-two"}
    requirements["resources"].append(second_db)
    register(db, store, registration(requirements=requirements)[0])
    topology = publisher(db, store, tmp_path).publish(MIGRATION)
    assert topology["code"] == "UNSUPPORTED_TOPOLOGY"
    assert revisions(db) == 0


def test_render_failure_is_not_published(db, store, tmp_path, onprem):
    result = publisher(db, store, tmp_path, helm_bin=str(tmp_path / "no-helm")).publish(MIGRATION)
    assert result["status"] == "FAILED" and result["code"] == "RENDER_FAILED"
    assert revisions(db) == 0


def test_missing_registration_is_a_structured_failure(db, store, tmp_path):
    result = publisher(db, store, tmp_path).publish("unknown-migration")
    assert result == {
        "status": "FAILED",
        "code": "PLANNING_INPUT_MISSING",
        "issues": ["PLANNING_INPUT_MISSING"],
    }


async def test_activity_without_store_stays_scaffold():
    activity = PlanPublishActivities(None)
    result = await ActivityEnvironment().run(
        activity.publish_migration_plan, {"migration_id": MIGRATION}
    )
    assert result == {"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"}


# --- Preview API → approval → execution -----------------------------------------------------


@pytest.fixture
def client(db, store, monkeypatch):
    monkeypatch.setattr(settings, "approval_admin_key", ADMIN)
    delivery = FakeDelivery()

    def session():
        with db() as s:
            yield s

    from cmp_api.routes import plan_inputs as routes

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[routes.get_session] = session
    app.dependency_overrides[routes.get_artifact_store] = lambda: store
    app.dependency_overrides[get_approval_delivery] = lambda: delivery
    yield TestClient(app)
    app.dependency_overrides.clear()


def approver(client, approver_id="alice", migrations=(MIGRATION,)):
    response = client.post(
        "/provisioning/approvers",
        json={"approver_id": approver_id, "migration_ids": list(migrations)},
        headers={"X-Approval-Admin-Key": ADMIN},
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['credential']}"}


def test_preview_api_then_approval_starts_onprem_execution(db, store, tmp_path, client, onprem):
    published = publisher(db, store, tmp_path).publish(MIGRATION)
    headers = approver(client)
    base = f"/provisioning/plans/{published['plan_id']}/revisions/1"
    response = client.get(f"{base}/preview", headers=headers)
    assert response.status_code == 200
    view = response.json()
    assert view["preview_digest"] == published["preview_digest"]
    assert view["latest"] is True and view["approvable"] is True and view["approval_issues"] == []
    assert view["preview"]["manifest_digest"] == view["manifest_digest"]
    latest = client.get(f"/provisioning/migrations/{MIGRATION}/plan", headers=headers)
    assert latest.status_code == 200 and latest.json() == view

    decision = client.post(
        f"{base}/approvals",
        json={
            "decision": "APPROVE",
            "scope": view["scope"],
            "manifest_digest": view["manifest_digest"],
            "preview_digest": view["preview_digest"],
        },
        headers=headers,
    )
    assert decision.status_code == 201, decision.text
    with db() as session:
        claim = claim_execution(session, decision.json()["approval_id"])
    assert claim["status"] == "CLAIMED" and claim["provider"] == "ON_PREM"
    # 12 starts: the job is queued for the (offline) fake Agent with the published artifacts.
    outcome = OnPremExecutionActivities(db, store, wait_seconds=0.05, poll_seconds=0.01)
    import asyncio

    result = asyncio.run(ActivityEnvironment().run(outcome.execute_on_prem_plan, claim))
    assert (result["status"], result["code"]) == ("UNKNOWN", "AGENT_OFFLINE")


def test_preview_api_requires_an_authorized_approver(db, store, tmp_path, client, onprem):
    published = publisher(db, store, tmp_path).publish(MIGRATION)
    base = f"/provisioning/plans/{published['plan_id']}/revisions/1/preview"
    assert client.get(base).status_code == 401
    other = approver(client, "bob", migrations=["other-migration"])
    assert client.get(base, headers=other).status_code == 403
    assert (
        client.get(f"/provisioning/migrations/{MIGRATION}/plan", headers=other).status_code == 403
    )
    headers = approver(client)
    missing = "/provisioning/plans/plan-unknown/revisions/1/preview"
    assert client.get(missing, headers=headers).status_code == 404
    assert client.get("/provisioning/migrations/none/plan", headers=headers).status_code == 404


def test_planning_input_route_requires_operator_and_rejects_mismatch(client):
    _, body = registration()
    url = f"/provisioning/migrations/{MIGRATION}/planning-input"
    assert client.put(url, json=body).status_code == 403
    response = client.put(url, json=body, headers={"X-Approval-Admin-Key": ADMIN})
    assert response.status_code == 200 and len(response.json()["input_digest"]) == 64
    wrong = {**body, "target": {"provider": "AWS", "settings": {}}}
    response = client.put(url, json=wrong, headers={"X-Approval-Admin-Key": ADMIN})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TARGET_PROVIDER_MISMATCH"
    tampered = {**body, "requirements_json": body["requirements_json"] + " "}
    response = client.put(url, json=tampered, headers={"X-Approval-Admin-Key": ADMIN})
    assert response.json()["detail"]["code"] == "REQUIREMENTS_DIGEST_MISMATCH"
    other = {**body, "planning_request": {**body["planning_request"], "migration_id": "x"}}
    response = client.put(url, json=other, headers={"X-Approval-Admin-Key": ADMIN})
    assert response.json()["detail"]["code"] == "MIGRATION_MISMATCH"


# --- AWS / GCP -------------------------------------------------------------------------------


def test_aws_input_publishes_infrastructure_plan_for_08(db, store, tmp_path, monkeypatch):
    terraform, _ = aws_tools(tmp_path, monkeypatch)
    register(db, store, registration("AWS")[0])
    pub = publisher(db, store, tmp_path, aws_profile="fixture", terraform_bin=str(terraform))
    result = pub.publish(MIGRATION)
    assert result["status"] == "PUBLISHED", result
    assert (result["provider"], result["scope"]) == ("AWS", "INFRASTRUCTURE")
    manifest, preview = stored(db, result)
    assert manifest.plan.template_version == "aws-eks-v2"
    assert approval_issues(manifest, preview) == []
    folder = tmp_path / "plans" / f"{result['plan_id']}-r1"
    assert (folder / "saved.tfplan").exists()  # 08 reads the saved Plan from here
    assert "TOP_SECRET_NEVER_SHOW" not in json.dumps(preview.model_dump(mode="json"))
    assert pub.publish(MIGRATION)["code"] == "PLAN_REUSED"


def test_aws_retry_after_lost_publish_reuses_plan_directory(db, store, tmp_path, monkeypatch):
    terraform, calls = aws_tools(tmp_path, monkeypatch)
    register(db, store, registration("AWS")[0])
    pub = publisher(db, store, tmp_path, aws_profile="fixture", terraform_bin=str(terraform))
    first = pub.publish(MIGRATION)
    with db() as session:  # simulate a Worker that planned but died before the DB commit
        session.query(PlanRevision).delete()
        from cmp_api.approvals import PlanPublication

        session.query(PlanPublication).delete()
        session.commit()
    plans_run = calls.read_text().count("plan -input")
    retried = pub.publish(MIGRATION)
    assert (
        retried["status"] == "PUBLISHED" and retried["manifest_digest"] == first["manifest_digest"]
    )
    assert calls.read_text().count("plan -input") == plans_run  # Terraform not re-run


def test_aws_plan_failure_and_location_mismatch_are_not_published(db, store, tmp_path, monkeypatch):
    terraform, _ = aws_tools(tmp_path, monkeypatch, fail_plan=True)
    register(db, store, registration("AWS")[0])
    pub = publisher(db, store, tmp_path, aws_profile="fixture", terraform_bin=str(terraform))
    result = pub.publish(MIGRATION)
    assert (result["status"], result["code"]) == ("FAILED", "PLAN_FAILED")
    assert result["issues"] == ["PLAN_COMMAND_FAILED"]
    assert "TOP_SECRET" not in json.dumps(result)
    payload, _ = planning_request()
    selection = {**payload["target_selection"], "provider": "AWS", "location": "us-east-1"}
    _, body = registration("AWS")
    body["planning_request"]["target_selection"] = selection
    with db() as session, pytest.raises(Exception, match="TARGET_LOCATION_MISMATCH"):
        register_planning_input(
            session, MIGRATION, PlanningInputRegistration.model_validate(body), store
        )
    assert revisions(db) == 0


def test_gcp_input_publishes_infrastructure_plan_for_10(db, store, tmp_path, monkeypatch):
    template, terraform, _ = gcp_tools(tmp_path, monkeypatch)
    register(db, store, registration("GCP")[0])
    pub = publisher(
        db,
        store,
        tmp_path,
        aws_profile="cmp-aws-prod",
        gcloud_configuration="cmp-gcp-prod",
        gcp_adc_email="planner@example.com",
        terraform_bin=str(terraform),
        gcp_template_dir=template,
    )
    result = pub.publish(MIGRATION)
    assert result["status"] == "PUBLISHED", result
    assert (result["provider"], result["scope"]) == ("GCP", "INFRASTRUCTURE")
    manifest, preview = stored(db, result)
    assert approval_issues(manifest, preview) == []
    with db() as session:
        assert session.get(PlanRevision, (result["plan_id"], 1)).migration_id == MIGRATION


def test_unconfigured_cloud_identity_is_a_structured_failure(db, store, tmp_path, monkeypatch):
    register(db, store, registration("AWS")[0])
    result = publisher(db, store, tmp_path).publish(MIGRATION)  # no AWS profile
    assert result == {"status": "FAILED", "code": "PLAN_FAILED", "issues": ["MISSING_PROFILE"]}


def test_pending_cleanup_decision_is_published_but_unapprovable(db, store, tmp_path):
    with db() as session:
        register_agent(session)
    payload, _ = planning_request()
    register(
        db, store, registration(sandbox={**payload["sandbox"], "cleanup_status": "PENDING"})[0]
    )
    result = publisher(db, store, tmp_path).publish(MIGRATION)
    manifest, preview = stored(db, result)
    assert [i.code for i in preview.pending_decisions] == ["CLEANUP_POLICY_REQUIRED"]
    assert {i.code for i in approval_issues(manifest, preview)} == {"PENDING_DECISION"}


def test_chart_package_is_production_code(tmp_path):
    from cmp_providers.chart_package import package_chart

    root = Path(__file__).resolve().parents[1]
    assert package_chart(root / "infra/helm/postgres")[:2] == b"\x1f\x8b"


def test_published_onprem_plan_holds_secret_references_only(db, store, tmp_path, onprem):
    result = publisher(db, store, tmp_path).publish(MIGRATION)
    assert set(result) == {
        "status", "code", "plan_id", "revision", "provider", "scope",
        "manifest_digest", "preview_digest",
    }  # fmt: skip
    manifest, _ = stored(db, result)
    db_values = json.loads(store.read(manifest.artifacts["database_values"]))
    assert db_values["database"]["secret_name"] == "postgres-main-auth"
    assert set(db_values["database"]) == {
        "name",
        "username",
        "secret_name",
        "secret_key",
        "version",
    }
    app_values = json.loads(store.read(manifest.artifacts["application_values"]))
    sources = {env["name"]: env["source"] for env in app_values["env"]}
    assert sources["DB_PASSWORD"] == {
        "kind": "SECRET", "name": "postgres-main-auth", "key": "password",
    }  # fmt: skip
    assert "LITERAL" not in json.dumps(sources["DB_PASSWORD"])
