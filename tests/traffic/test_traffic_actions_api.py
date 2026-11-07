"""#234: `POST /migrations/{id}/traffic/rollback|retry`.

단위: 가짜 TrafficControl로 404·409·202·503 응답. 통합: 시험용 Temporal에 부모 자리 Workflow와 진짜
TrafficShiftWorkflow(`traffic-<id>`)를 띄우고, API가 보낸 Signal로 실제로 되돌리고 다시 전환하는지 본다.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from cmp_api import temporal_client
from cmp_api.main import app
from cmp_api.routes import traffic_actions
from cmp_api.routes.traffic_actions import TrafficUnavailable, get_traffic_control
from cmp_contracts.queues import WORKFLOW_QUEUE
from cmp_worker.workflows.traffic import TrafficShiftWorkflow
from fastapi.testclient import TestClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_traffic_actions import Relay, Store, body, until


class FakeControl:
    def __init__(self, *, exists=True, actions=None, unavailable=False):
        self.exists = exists
        self.given = actions
        self.unavailable = unavailable
        self.signals: list[tuple[str, str]] = []

    async def migration_exists(self, migration_id: str) -> bool:
        if self.unavailable:
            raise TrafficUnavailable
        return self.exists

    async def actions(self, workflow_id: str):
        return self.given

    async def signal(self, workflow_id: str, name: str) -> None:
        self.signals.append((workflow_id, name))


@pytest.fixture
def control():
    holder: dict = {}

    def use(fake: FakeControl) -> TestClient:
        holder["fake"] = fake
        app.dependency_overrides[get_traffic_control] = lambda: fake
        return TestClient(app)

    yield use
    app.dependency_overrides.pop(get_traffic_control, None)


def test_rollback_while_shifting_signals_the_traffic_child(control) -> None:
    fake = FakeControl(actions={"rollback": True, "retry": False})
    response = control(fake).post("/migrations/migration-1/traffic/rollback")
    assert response.status_code == 202
    assert response.json() == {
        "migration_id": "migration-1",
        "workflow_id": "traffic-migration-1",
        "action": "rollback",
        "signal": "request_rollback",
    }
    assert fake.signals == [("traffic-migration-1", "request_rollback")]


def test_retry_after_a_rollback_signals_the_traffic_child(control) -> None:
    fake = FakeControl(actions={"rollback": False, "retry": True})
    response = control(fake).post("/migrations/migration-1/traffic/retry")
    assert response.status_code == 202 and response.json()["signal"] == "request_retry"
    assert fake.signals == [("traffic-migration-1", "request_retry")]


def test_finalize_while_stabilizing_signals_the_traffic_child(control) -> None:
    fake = FakeControl(actions={"rollback": True, "retry": False, "finalize": True})
    response = control(fake).post("/migrations/migration-1/traffic/finalize")
    assert response.status_code == 202 and response.json()["signal"] == "request_finalize"
    assert fake.signals == [("traffic-migration-1", "request_finalize")]


@pytest.mark.parametrize("action", ["rollback", "retry", "finalize"])
def test_unknown_migration_is_404(control, action) -> None:
    fake = FakeControl(exists=False)
    response = control(fake).post(f"/migrations/nope/traffic/{action}")
    assert response.status_code == 404 and response.json()["detail"] == "MIGRATION_NOT_FOUND"
    assert fake.signals == []


@pytest.mark.parametrize(
    ("action", "actions", "code"),
    [
        ("rollback", None, "TRAFFIC_NOT_RUNNING"),  # not started yet or already finished
        ("retry", None, "TRAFFIC_NOT_RUNNING"),
        ("rollback", {"rollback": False, "retry": True}, "TRAFFIC_ACTION_NOT_ALLOWED"),
        ("retry", {"rollback": True, "retry": False}, "TRAFFIC_ACTION_NOT_ALLOWED"),
        # 안정화 구간이 아니면(전환 중) 확정할 수 없다
        ("finalize", {"rollback": True, "retry": False}, "TRAFFIC_ACTION_NOT_ALLOWED"),
    ],
)
def test_wrong_state_is_409_with_a_code_and_no_signal(control, action, actions, code) -> None:
    fake = FakeControl(actions=actions)
    response = control(fake).post(f"/migrations/migration-1/traffic/{action}")
    assert response.status_code == 409 and response.json()["detail"] == code
    assert fake.signals == []


def test_unreachable_temporal_is_503(control) -> None:
    response = control(FakeControl(unavailable=True)).post("/migrations/m/traffic/rollback")
    assert response.status_code == 503 and response.json()["detail"] == "TRAFFIC_UNAVAILABLE"


def test_routes_are_in_the_openapi_contract() -> None:
    paths = app.openapi()["paths"]
    assert "post" in paths["/migrations/{migration_id}/traffic/rollback"]
    assert "post" in paths["/migrations/{migration_id}/traffic/retry"]
    assert "post" in paths["/migrations/{migration_id}/traffic/finalize"]


# ---- 통합: 진짜 Temporal Signal ----


async def test_api_rolls_back_and_retries_a_real_traffic_workflow(monkeypatch) -> None:
    store, relay = Store({}), Relay(hold=True)
    migration_id = f"migration-{uuid.uuid4()}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[TrafficShiftWorkflow],
            activities=store.activities(),
        ),
        relay.worker(env.client),
    ):

        async def client():
            return env.client

        monkeypatch.setattr(temporal_client, "get_temporal_client", client)
        monkeypatch.setattr(traffic_actions, "get_temporal_client", client)
        # Stand-in for the parent `migration-<id>`: a run that ends at once still exists (404 check).
        stand_in = body(None, handover={"status": "UNKNOWN", "ready": False})
        await env.client.execute_workflow(
            TrafficShiftWorkflow.run, stand_in, id=migration_id, task_queue=WORKFLOW_QUEUE
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as api:
            url = f"/migrations/{migration_id}/traffic"
            assert (await api.post(f"{url}/rollback")).status_code == 409  # not started

            handle = await env.client.start_workflow(
                TrafficShiftWorkflow.run,
                body(migration_id),
                id=f"traffic-{migration_id}",
                task_queue=WORKFLOW_QUEUE,
            )
            await until(handle, lambda s: s["actions"]["rollback"])
            assert (await api.post(f"{url}/retry")).status_code == 409  # still shifting

            assert (await api.post(f"{url}/rollback")).status_code == 202
            await until(handle, lambda s: s["actions"]["retry"])
            assert (await api.post(f"{url}/rollback")).status_code == 409  # already back

            relay.release.set()
            assert (await api.post(f"{url}/retry")).status_code == 202
            assert (await handle.result())["status"] == "DONE"
            assert (await api.post(f"{url}/rollback")).status_code == 409  # finished
            assert (await api.post("/migrations/migration-nope/traffic/retry")).status_code == 404
    assert relay.calls.count(("rollback", None)) == 1
