"""Ticket 07 seam: approval HTTP request + business DB + Temporal workflow + fake executor."""

from pathlib import Path

import pytest
from cmp_api.approvals import publish_plan_revision
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.routes.approvals import get_approval_delivery, get_session
from cmp_contracts.plan import PlanManifest, Preview
from cmp_providers.artifacts import sha256_json
from cmp_providers.plans import manifest_digest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

FIXTURES = Path(__file__).parent / "fixtures/contracts"
ADMIN_KEY = "test-approval-admin-key"
BASE = "/provisioning/plans/plan-001/revisions"


def manifest(revision=1, account="123456789012"):
    payload = PlanManifest.model_validate_json((FIXTURES / "aws-plan.json").read_text())
    data = payload.model_dump(mode="json")
    data["plan"]["revision"] = revision
    data["target"]["account_id"] = account
    return PlanManifest.model_validate(data)


def preview(plan, **extra):
    return Preview(
        contract_version="1",
        manifest_digest=manifest_digest(plan),
        changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "demo"}],
        **extra,
    )


def body(plan, display, decision="APPROVE", **override):
    payload = {
        "decision": decision,
        "scope": plan.scope,
        "manifest_digest": manifest_digest(plan),
        "preview_digest": sha256_json(display.model_dump(mode="json")),
    }
    payload.update(override)
    return payload


class FakeDelivery:
    """Delivery port test double; the real one uses Temporal Signal-With-Start."""

    def __init__(self):
        self.fail = False
        self.delivered = []

    async def deliver(self, *, approval_id, plan_id, revision):
        if self.fail:
            raise ConnectionError("temporal unavailable")
        self.delivered.append(approval_id)

    async def workflow_state(self, *, plan_id, revision):
        return "AWAITING_APPROVAL" if not self.delivered else "APPROVAL_RECEIVED"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def delivery():
    return FakeDelivery()


@pytest.fixture
def client(db, delivery, monkeypatch):
    monkeypatch.setattr(settings, "approval_admin_key", ADMIN_KEY)

    def session():
        with db() as s:
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_approval_delivery] = lambda: delivery
    yield TestClient(app)
    app.dependency_overrides.clear()


def publish(db, plan, display):
    with db() as s:
        publish_plan_revision(s, plan, display)


def approver(client, approver_id="alice", migrations=("migration-001",)):
    response = client.post(
        "/provisioning/approvers",
        json={"approver_id": approver_id, "migration_ids": list(migrations)},
        headers={"X-Approval-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['credential']}"}


def test_approver_registration_requires_operator_key(client):
    response = client.post(
        "/provisioning/approvers",
        json={"approver_id": "alice", "migration_ids": ["migration-001"]},
        headers={"X-Approval-Admin-Key": "wrong"},
    )
    assert response.status_code == 403


def test_approval_is_recorded_then_delivered_as_separate_states(client, db, delivery):
    plan = manifest()
    display = preview(plan)
    publish(db, plan, display)
    response = client.post(
        f"{BASE}/1/approvals", json=body(plan, display), headers=approver(client)
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["decision"] == "APPROVE"
    assert result["approver_id"] == "alice"
    assert result["delivery_status"] == "DELIVERED"
    assert result["execution_status"] == "NOT_STARTED"
    assert result["workflow_state"] == "APPROVAL_RECEIVED"
    assert delivery.delivered == [result["approval_id"]]


def test_approver_identity_comes_from_credential_not_body(client, db):
    plan = manifest()
    display = preview(plan)
    publish(db, plan, display)
    response = client.post(
        f"{BASE}/1/approvals",
        json=body(plan, display, approver_id="mallory"),
        headers=approver(client),
    )
    assert response.status_code == 422  # identity in the body is never accepted
    response = client.post(f"{BASE}/1/approvals", json=body(plan, display))
    assert response.status_code == 401
    assert response.json()["detail"] == "APPROVER_AUTH_REQUIRED"
    response = client.post(
        f"{BASE}/1/approvals",
        json=body(plan, display),
        headers={"Authorization": "Bearer forged"},
    )
    assert response.status_code == 401


def test_approver_without_migration_grant_is_forbidden(client, db, delivery):
    plan = manifest()
    display = preview(plan)
    publish(db, plan, display)
    headers = approver(client, "bob", migrations=["other-migration"])
    response = client.post(f"{BASE}/1/approvals", json=body(plan, display), headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "APPROVER_FORBIDDEN"
    assert delivery.delivered == []


def test_unknown_plan_is_not_found(client):
    response = client.post(
        f"{BASE}/1/approvals",
        json={
            "decision": "APPROVE",
            "scope": "FULL",
            "manifest_digest": "0" * 64,
            "preview_digest": "0" * 64,
        },
        headers=approver(client),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "PLAN_NOT_FOUND"


def test_stale_revision_cannot_be_approved(client, db, delivery):
    old = manifest(1)
    old_preview = preview(old)
    publish(db, old, old_preview)
    new = manifest(2)
    publish(db, new, preview(new))
    response = client.post(
        f"{BASE}/1/approvals", json=body(old, old_preview), headers=approver(client)
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "STALE_REVISION"
    assert delivery.delivered == []


@pytest.mark.parametrize("field", ["manifest_digest", "preview_digest", "scope"])
def test_client_target_must_match_stored_plan(client, db, delivery, field):
    plan = manifest()
    display = preview(plan)
    publish(db, plan, display)
    changed = {"scope": "WORKLOADS"} if field == "scope" else {field: "f" * 64}
    response = client.post(
        f"{BASE}/1/approvals", json=body(plan, display, **changed), headers=approver(client)
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "APPROVAL_TARGET_MISMATCH"
    assert delivery.delivered == []


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("pending_decisions", "LOCAL_STATE_VALIDATION_ONLY"),
        ("errors", "LOCAL_STATE_UNAPPROVABLE"),
    ],
)
def test_local_state_validation_preview_is_not_approvable(client, db, delivery, kind, code):
    plan = manifest()
    issue = {"code": code, "path": "terraform.backend", "message": "Local validation state."}
    display = preview(plan, **{kind: [issue]})
    publish(db, plan, display)
    response = client.post(
        f"{BASE}/1/approvals", json=body(plan, display), headers=approver(client)
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PLAN_NOT_APPROVABLE"
    assert {"VALIDATION_FAILED", "PENDING_DECISION"} & set(detail["issues"])
    assert delivery.delivered == []


def test_repeated_same_approval_returns_existing_record(client, db, delivery):
    plan = manifest()
    display = preview(plan)
    publish(db, plan, display)
    headers = approver(client)
    first = client.post(f"{BASE}/1/approvals", json=body(plan, display), headers=headers)
    second = client.post(f"{BASE}/1/approvals", json=body(plan, display), headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["approval_id"] == first.json()["approval_id"]


def test_conflicting_decision_for_same_revision_is_rejected(client, db):
    plan = manifest()
    display = preview(plan)
    publish(db, plan, display)
    headers = approver(client)
    client.post(f"{BASE}/1/approvals", json=body(plan, display, "REJECT"), headers=headers)
    response = client.post(f"{BASE}/1/approvals", json=body(plan, display), headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "ALREADY_DECIDED"


def test_signal_failure_after_db_record_is_visible_and_recoverable(client, db, delivery):
    plan = manifest()
    display = preview(plan)
    publish(db, plan, display)
    headers = approver(client)
    delivery.fail = True
    response = client.post(f"{BASE}/1/approvals", json=body(plan, display), headers=headers)
    assert response.status_code == 201
    failed = response.json()
    assert failed["delivery_status"] == "FAILED"
    assert failed["delivery_attempts"] == 1
    assert failed["execution_status"] == "NOT_STARTED"
    approval_id = failed["approval_id"]

    lookup = client.get(f"{BASE}/1/approvals/{approval_id}", headers=headers)
    assert lookup.status_code == 200
    assert lookup.json()["delivery_status"] == "FAILED"

    delivery.fail = False
    retried = client.post(f"{BASE}/1/approvals/{approval_id}/delivery", headers=headers)
    assert retried.status_code == 200
    assert retried.json()["delivery_status"] == "DELIVERED"
    assert retried.json()["delivery_attempts"] == 2
    assert delivery.delivered == [approval_id]


def test_repeated_request_also_retries_failed_delivery(client, db, delivery):
    plan = manifest()
    display = preview(plan)
    publish(db, plan, display)
    headers = approver(client)
    delivery.fail = True
    first = client.post(f"{BASE}/1/approvals", json=body(plan, display), headers=headers)
    delivery.fail = False
    second = client.post(f"{BASE}/1/approvals", json=body(plan, display), headers=headers)
    assert second.json()["approval_id"] == first.json()["approval_id"]
    assert second.json()["delivery_status"] == "DELIVERED"


def test_plan_revision_is_immutable_and_increasing(db):
    plan = manifest(1)
    publish(db, plan, preview(plan))
    with db() as s, pytest.raises(ValueError, match="REVISION_NOT_INCREASING"):
        publish_plan_revision(s, plan, preview(plan))
