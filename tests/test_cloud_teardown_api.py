"""Ticket 53 (D1, D2, D5, D11) seam: operator teardown HTTP routes with a fake Temporal client."""

import pytest
from cmp_api.approvals import PlanExecution
from cmp_api.config import settings
from cmp_api.main import app
from cmp_api.routes.cloud_teardown import get_client, get_session
from fastapi.testclient import TestClient
from temporalio.exceptions import WorkflowAlreadyStartedError
from test_aws_workloads import db  # noqa: F401 - fixture
from test_cloud_teardown import executed

ADMIN = {"X-Enrollment-Admin-Key": "admin"}
DIGEST = "d" * 64


class FakeHandle:
    def __init__(self, client, workflow_id):
        self.client, self.id = client, workflow_id

    async def query(self, name):
        return self.client.status[self.id]

    async def signal(self, name, value):
        self.client.signals.append((self.id, name, value))


class FakeClient:
    def __init__(self):
        self.started, self.signals, self.status = [], [], {}

    async def start_workflow(self, name, request, *, id, task_queue):
        if id in self.status:
            raise WorkflowAlreadyStartedError(id, name)
        self.started.append((name, request, id, task_queue))
        self.status[id] = {"phase": "WORKLOADS", "delete_data": request["delete_data"]}

    def get_workflow_handle(self, workflow_id):
        return FakeHandle(self, workflow_id)


@pytest.fixture
def temporal():
    return FakeClient()


@pytest.fixture
def client(db, temporal, monkeypatch):  # noqa: F811
    def session():
        with db() as value:
            yield value

    monkeypatch.setattr(settings, "agent_enrollment_admin_key", "admin")
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_client] = lambda: temporal
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_admin_key_is_required(client):
    assert client.post("/provisioning-admin/teardowns", json={}).status_code == 403
    assert client.get("/provisioning-admin/teardowns/cloud-teardown-x-r1").status_code == 403


def test_start_show_and_confirm_the_reviewed_destroy(tmp_path, db, client, temporal):  # noqa: F811
    executed(tmp_path, db)
    started = client.post(
        "/provisioning-admin/teardowns",
        json={"plan_id": "plan-aws", "revision": 1, "delete_data": True},
        headers=ADMIN,
    )
    assert started.status_code == 202
    teardown = started.json()["teardown_id"]
    assert teardown == "cloud-teardown-plan-aws-r1"
    name, request, _, _ = temporal.started[0]
    assert name == "CloudTeardownWorkflow" and request["delete_data"] is True

    again = client.post("/provisioning-admin/teardowns",
                        json={"plan_id": "plan-aws", "revision": 1}, headers=ADMIN)  # fmt: skip
    assert (again.status_code, again.json()["detail"]) == (409, "TEARDOWN_IN_PROGRESS")

    url = f"/provisioning-admin/teardowns/{teardown}"
    early = client.post(f"{url}/confirm", json={"review_digest": DIGEST}, headers=ADMIN)
    assert (early.status_code, early.json()["detail"]) == (409, "NOT_AWAITING_CONFIRMATION")

    review = {"digest": DIGEST, "addresses": ["aws_vpc.target"], "unprotect": False}
    temporal.status[teardown] = {"phase": "AWAITING_CONFIRMATION", "review": review}
    assert client.get(url, headers=ADMIN).json()["review"] == review
    wrong = client.post(f"{url}/confirm", json={"review_digest": "0" * 64}, headers=ADMIN)
    assert (wrong.status_code, wrong.json()["detail"]) == (409, "DESTROY_REVIEW_MISMATCH")
    assert not temporal.signals
    ok = client.post(f"{url}/confirm", json={"review_digest": DIGEST}, headers=ADMIN)
    assert ok.status_code == 202
    assert temporal.signals == [(teardown, "confirm_destroy", DIGEST)]


def test_only_executed_infrastructure_revisions_qualify(tmp_path, db, client):  # noqa: F811
    executed(tmp_path, db)
    cases = {
        ("missing", 1): (404, "PLAN_NOT_FOUND"),
        ("plan-009", 1): (409, "UNSUPPORTED_EXECUTION_SCOPE"),  # WORKLOADS Plan
    }
    for (plan_id, revision), expected in cases.items():
        response = client.post("/provisioning-admin/teardowns",
                               json={"plan_id": plan_id, "revision": revision}, headers=ADMIN)  # fmt: skip
        assert (response.status_code, response.json()["detail"]) == expected


def test_running_execution_of_the_same_target_blocks_teardown(tmp_path, db, client):  # noqa: F811
    executed(tmp_path, db)
    with db() as session:
        session.get(PlanExecution, ("plan-009", 1, "PROVISION")).status = "RUNNING"
        session.commit()
    response = client.post("/provisioning-admin/teardowns",
                           json={"plan_id": "plan-aws", "revision": 1}, headers=ADMIN)  # fmt: skip
    assert (response.status_code, response.json()["detail"]) == (409, "EXECUTION_IN_PROGRESS")


def test_other_workflows_cannot_be_read_through_the_teardown_route(client):
    response = client.get("/provisioning-admin/teardowns/migration-123", headers=ADMIN)
    assert response.status_code == 404
