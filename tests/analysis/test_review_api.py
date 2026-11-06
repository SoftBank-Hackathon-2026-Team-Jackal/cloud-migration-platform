"""분석 검토 게이트 API(`/migrations/{id}/analysis/review`).

단위: 가짜 ReviewControl로 404·409·422·503·202 응답과 Signal 내용. 통합: 시험용 Temporal에 진짜
AnalysisWorkflow를 띄우고, API만으로 확정 → 승인 → 분석 끝까지 가는지, 잘못된 결정은 거절되는지 본다.
"""

from __future__ import annotations

import httpx
import pytest
from cmp_api import temporal_client
from cmp_api.main import app
from cmp_api.routes import analysis_review
from cmp_api.routes.analysis_review import ReviewUnavailable, get_review_control
from fastapi.testclient import TestClient
from test_review_gate import REF, ROWS, running, start, until

PATH = "/migrations/mig-1/analysis/review"
VIEW = {
    "phase": "WAITING_CONFIRMATION",
    "analysis_ref": {"id": "analysis", "version": "1"},
    "pending": "confirm_requirements",
    "items": [{"id": "a"}, {"id": "b"}],
    "security": [],
    "blocked": [],
    "rejections": [],
}


class FakeControl:
    """`states`는 호출 순서대로 돌려주고 마지막 값을 유지한다(Signal 뒤 상태 변화를 흉내 낸다)."""

    def __init__(self, *states, exists=True, unavailable=False):
        self.states = list(states)
        self.exists = exists
        self.unavailable = unavailable
        self.signals: list[tuple[str, str, dict]] = []

    async def migration_exists(self, migration_id: str) -> bool:
        if self.unavailable:
            raise ReviewUnavailable
        return self.exists

    async def state(self, migration_id: str):
        if self.unavailable:
            raise ReviewUnavailable
        if not self.states:
            return None
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def signal(self, migration_id: str, name: str, payload: dict) -> None:
        self.signals.append((migration_id, name, payload))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(analysis_review, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(analysis_review, "SETTLE_TRIES", 3)
    holder: dict = {}

    def use(fake: FakeControl) -> TestClient:
        holder["fake"] = fake
        app.dependency_overrides[get_review_control] = lambda: fake
        return TestClient(app)

    yield use
    app.dependency_overrides.pop(get_review_control, None)


def moved(**changes):
    return {**VIEW, **changes}


CONFIRM = {"analysis_version": "1", "required": {"a": True, "b": False}}
APPROVE = {"analysis_version": "1", "approvals": {"b": "APPROVED"}}
WAITING_APPROVAL = {"phase": "WAITING_APPROVAL", "pending": "approve_losses"}


def test_get_returns_the_review_view(client) -> None:
    response = client(FakeControl(VIEW)).get(PATH)
    assert response.status_code == 200 and response.json() == VIEW


def test_get_while_analysis_is_still_running_has_nothing_pending(client) -> None:
    running_state = moved(phase="ANALYZING", analysis_ref=None, pending=None, items=[])
    body = client(FakeControl(running_state)).get(PATH).json()
    assert body["pending"] is None and body["items"] == []


def test_unknown_migration_and_missing_analysis_are_404(client) -> None:
    response = client(FakeControl(exists=False)).get(PATH)
    assert (response.status_code, response.json()["detail"]) == (404, "MIGRATION_NOT_FOUND")
    response = client(FakeControl()).get(PATH)  # the migration exists, no analysis child yet
    assert (response.status_code, response.json()["detail"]) == (404, "ANALYSIS_NOT_FOUND")


def test_temporal_down_is_503(client) -> None:
    for call in (
        lambda c: c.get(PATH),
        lambda c: c.post(PATH + "/confirm", json=CONFIRM),
    ):
        response = call(client(FakeControl(VIEW, unavailable=True)))
        assert (response.status_code, response.json()["detail"]) == (503, "REVIEW_UNAVAILABLE")


def test_confirm_signals_with_the_stored_analysis_ref_and_default_actor(client) -> None:
    fake = FakeControl(VIEW, moved(**WAITING_APPROVAL))
    response = client(fake).post(PATH + "/confirm", json=CONFIRM)
    assert response.status_code == 202
    assert response.json() == {
        "migration_id": "mig-1",
        "signal": "confirm_requirements",
        "phase": "WAITING_APPROVAL",
        "pending": "approve_losses",
    }
    assert fake.signals == [
        (
            "mig-1",
            "confirm_requirements",
            {
                "analysis_ref": {"id": "analysis", "version": "1"},
                "required": {"a": True, "b": False},
                "operational": [],
                "actor": "web-user",
            },
        )
    ]


def test_actor_is_taken_from_the_request_when_given(client) -> None:
    fake = FakeControl(VIEW, moved(**WAITING_APPROVAL))
    client(fake).post(PATH + "/confirm", json={**CONFIRM, "actor": "sanghun"})
    assert fake.signals[0][2]["actor"] == "sanghun"


def test_confirm_that_finishes_the_gate_reports_nothing_pending(client) -> None:
    done = moved(phase="DONE", pending=None)
    response = client(FakeControl(VIEW, done)).post(PATH + "/confirm", json=CONFIRM)
    assert response.status_code == 202 and response.json()["pending"] is None


def test_approve_signals_when_the_gate_waits_for_approval(client) -> None:
    waiting = moved(**WAITING_APPROVAL)
    fake = FakeControl(waiting, moved(phase="DONE", pending=None))
    response = client(fake).post(PATH + "/approve", json=APPROVE)
    assert response.status_code == 202 and response.json()["signal"] == "approve_losses"
    assert fake.signals[0][2]["approvals"] == {"b": "APPROVED"}


def test_a_decision_the_gate_is_not_waiting_for_is_409_and_sends_nothing(client) -> None:
    fake = FakeControl(VIEW)  # waits for confirm, not approve
    response = client(fake).post(PATH + "/approve", json=APPROVE)
    assert (response.status_code, response.json()["detail"]) == (409, "REVIEW_NOT_PENDING")
    assert fake.signals == []


def test_another_analysis_version_is_409_and_sends_nothing(client) -> None:
    fake = FakeControl(VIEW)
    response = client(fake).post(PATH + "/confirm", json={**CONFIRM, "analysis_version": "2"})
    assert (response.status_code, response.json()["detail"]) == (409, "ANALYSIS_REF_MISMATCH")
    assert fake.signals == []


def test_a_decision_the_gate_refuses_is_422_with_its_code(client) -> None:
    refused = moved(rejections=[{"signal": "confirm_requirements", "code": "MISSING_ROW"}])
    fake = FakeControl(VIEW, refused)
    response = client(fake).post(PATH + "/confirm", json=CONFIRM)
    assert (response.status_code, response.json()["detail"]) == (422, "MISSING_ROW")


def test_a_decision_not_yet_seen_is_still_accepted_as_pending(client) -> None:
    response = client(FakeControl(VIEW)).post(PATH + "/confirm", json=CONFIRM)
    assert response.status_code == 202 and response.json()["phase"] == "PENDING"


def test_an_empty_confirmation_is_sent_when_only_a_security_approval_is_left(client) -> None:
    """No mapping rows, only a lowered security level: there is nothing to mark required, so the
    confirmation is empty and the gate moves on to the approval (the CLI note says this was untried)."""
    fake = FakeControl(VIEW, moved(**WAITING_APPROVAL))
    body = {"analysis_version": "1", "required": {}}
    response = client(fake).post(PATH + "/confirm", json=body)
    assert response.status_code == 202 and response.json()["pending"] == "approve_losses"
    assert fake.signals[0][2]["required"] == {}


def test_the_real_gate_accepts_an_empty_confirmation_when_it_has_no_rows() -> None:
    from cmp_worker.review_gate import ReviewGate

    judgement = {
        "criterion": "EXTERNAL_HTTPS", "source_state": "PROTECTED", "target_state": "UNPROTECTED",
        "verdict": "LOWER", "weak_source": False, "reasons": ["TARGET_ENTRY_WITHOUT_TLS"],
        "suggestion": None,
    }  # fmt: skip
    gate = ReviewGate({"id": "analysis", "version": "1"}, [], security=[judgement])
    assert gate.confirm(
        {"analysis_ref": {"id": "analysis", "version": "1"}, "required": {}, "actor": "a"}
    )
    assert gate.phase == "WAITING_APPROVAL"
    assert gate.view()["pending"] == "approve_losses"


@pytest.mark.parametrize(
    "body",
    [
        {"analysis_version": "1"},  # no decisions
        {"analysis_version": "1", "required": {"a": "yes"}},  # strict: bool only
        {**CONFIRM, "extra": 1},
        {"required": {"a": True}},  # version is required
    ],
)
def test_malformed_confirm_bodies_are_422(client, body) -> None:
    fake = FakeControl(VIEW)
    assert client(fake).post(PATH + "/confirm", json=body).status_code == 422
    assert fake.signals == []


def test_approval_decisions_are_only_approved_or_rejected(client) -> None:
    fake = FakeControl(moved(**WAITING_APPROVAL))
    body = {"analysis_version": "1", "approvals": {"b": "MAYBE"}}
    assert client(fake).post(PATH + "/approve", json=body).status_code == 422
    assert fake.signals == []


# ---- 통합: 진짜 AnalysisWorkflow ----


async def test_the_api_alone_takes_the_real_gate_from_confirm_to_the_end(monkeypatch) -> None:
    async def parent_exists(self, migration_id: str) -> bool:
        return True  # the parent MigrationWorkflow is not part of this test

    monkeypatch.setattr(analysis_review.TemporalReviewControl, "migration_exists", parent_exists)
    monkeypatch.setattr(analysis_review, "SETTLE_SECONDS", 0.05)
    path = "/migrations/mig/analysis/review"  # `start` runs the child as `analysis-mig`
    async with running(ROWS) as env:

        async def get_client():
            return env.client

        monkeypatch.setattr(temporal_client, "get_temporal_client", get_client)
        monkeypatch.setattr(analysis_review, "get_temporal_client", get_client)
        handle = await start(env.client)
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as api:
            view = (await api.get(path)).json()
            assert view["pending"] == "confirm_requirements"
            assert {item["id"] for item in view["items"]} == {"a", "b", "c"}

            # a missing row is refused by the real gate and reported as 422
            bad = await api.post(
                path + "/confirm",
                json={"analysis_version": REF["version"], "required": {"a": True}},
            )
            assert (bad.status_code, bad.json()["detail"]) == (422, "MISSING_ROW")
            assert (await handle.query("review_state"))["pending"] == "confirm_requirements"

            ok = await api.post(
                path + "/confirm",
                json={
                    "analysis_version": REF["version"],
                    "required": {"a": True, "b": False, "c": False},
                    "actor": "sanghun",
                },
            )
            assert ok.status_code == 202 and ok.json()["pending"] == "approve_losses"

            # only the rows the gate marked APPROVAL need a decision; a partial answer is refused
            asking = [
                i["id"]
                for i in (await api.get(path)).json()["items"]
                if i["treatment"] == "APPROVAL"
            ]
            assert asking
            partial = await api.post(
                path + "/approve",
                json={"analysis_version": REF["version"], "approvals": {"zzz": "APPROVED"}},
            )
            assert (partial.status_code, partial.json()["detail"]) == (422, "UNKNOWN_ROW")
            done = await api.post(
                path + "/approve",
                json={
                    "analysis_version": REF["version"],
                    "approvals": {row: "APPROVED" for row in asking},
                    "actor": "sanghun",
                },
            )
            assert done.status_code == 202 and done.json()["pending"] is None
        record = (await handle.result())["review"]
        assert record["actor"] == "sanghun" and set(record["approvals"]) == set(
            asking
        )  # the record names who decided
