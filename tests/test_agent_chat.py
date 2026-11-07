"""에이전트 채팅 `POST /agent/ask`: 진행 신호·답·자막, 비용 장부와 긴급 중지, 입력 다듬기, 공개 엣지에 없음.

모델과 장부는 가짜로 대신한다(실제 호출·실제 장부는 서버에서만).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cmp_api.agent_chat import AgentChat, AgentSettings, AgentUnavailable, clean
from cmp_api.main import app
from cmp_api.routes.agent import get_agent_chat
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


class FakeModel:
    def __init__(self, *answers: str, fail: Exception | None = None) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail

    def complete(self, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.calls.append((system, user, model))
        if self.fail:
            raise self.fail
        return self.answers.pop(0), 120, 40


class BudgetError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FakeLedger:
    def __init__(self, *, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.reserved: list[tuple[str, int]] = []
        self.settled: list[tuple[str, int, int]] = []

    def reserve(self, run: str, worst_case_micro: int) -> int:
        if self.refuse:
            raise BudgetError(self.refuse)
        self.reserved.append((run, worst_case_micro))
        return worst_case_micro

    def settle(self, run: str, reserved_micro: int, actual_micro: int) -> None:
        self.settled.append((run, reserved_micro, actual_micro))


ON = AgentSettings(enabled=True)


def chat(model: FakeModel, ledger: FakeLedger | None = None, *, settings=ON, key=True) -> AgentChat:
    return AgentChat(settings, model=model, ledger=ledger or FakeLedger(), has_key=lambda: key)


def parse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def run(agent: AgentChat, question="지금 무슨 단계야?", **fields) -> list[tuple[str, dict]]:
    request = clean(
        question,
        fields.get("context", "stage: analysis"),
        fields.get("lang", "ko"),
        fields.get("caption"),
    )
    return parse("".join([block async for block in agent.ask(request)]))


# ---- 입력 다듬기 ----


def test_clean_trims_and_defaults() -> None:
    request = clean("  뭐가 문제야?  ", "x" * 7000, "fr", "ko")
    assert request.question == "뭐가 문제야?" and len(request.context) == 6000
    assert (
        request.lang == "ko" and request.caption_lang is None
    )  # 모르는 언어는 한국어, 같은 언어 자막은 뺀다


def test_clean_keeps_a_different_caption_language() -> None:
    assert clean("q", "", "ko", "ja").caption_lang == "ja"
    assert clean("q", "", "en", "ko").lang == "en"


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_question_is_refused(empty) -> None:
    with pytest.raises(ValueError):
        clean(empty, "", "ko", None)


# ---- 진행 신호와 답 ----


async def test_progress_signals_come_first_then_the_answer() -> None:
    model = FakeModel("분석 단계이고 문제는 없습니다.")
    events = await run(chat(model))
    assert [name for name, _ in events] == ["status", "status", "answer", "done"]
    assert [data["step"] for name, data in events if name == "status"] == [
        "reading_state",
        "asking_model",
    ]
    assert events[2][1] == {"answer": "분석 단계이고 문제는 없습니다.", "provider": "platform"}


async def test_the_model_gets_the_state_and_the_question_and_the_language() -> None:
    model = FakeModel("ok")
    await run(
        chat(model), "로그를 읊어 줘", context="stage: traffic\n최근 기록: 트래픽 10%", lang="en"
    )
    system, user, name = model.calls[0]
    assert "stage: traffic" in user and "로그를 읊어 줘" in user
    assert "English" in system and name == "claude-haiku-5-5"


async def test_a_caption_is_translated_when_the_screen_language_differs() -> None:
    model = FakeModel("문제 없습니다.", "問題ありません。")
    events = await run(chat(model), caption="ja")
    assert [data.get("step") for name, data in events if name == "status"] == [
        "reading_state",
        "asking_model",
        "translating",
    ]
    assert events[-2][1]["caption"] == "問題ありません。"
    assert (
        "日本語" in model.calls[1][0] and model.calls[1][1] == "문제 없습니다."
    )  # 번역 입력은 답 그대로


async def test_a_failed_translation_only_drops_the_caption() -> None:
    class Flaky(FakeModel):
        def complete(self, system, user, model):
            if len(self.calls) == 1:
                self.calls.append((system, user, model))
                raise AgentUnavailable("LLM_UPSTREAM")
            return super().complete(system, user, model)

    events = await run(chat(Flaky("답입니다.")), caption="ja")
    assert events[-2][0] == "answer" and "caption" not in events[-2][1] and events[-1][0] == "done"


# ---- 쓸 수 없을 때 ----


@pytest.mark.parametrize(
    ("settings", "key", "ledger", "code"),
    [
        (AgentSettings(enabled=False), True, True, "AGENT_DISABLED"),
        (ON, False, True, "AGENT_NO_KEY"),
        (ON, True, False, "AGENT_NO_LEDGER"),
    ],
)
async def test_an_agent_that_is_off_or_unconfigured_answers_with_one_error(
    settings, key, ledger, code
) -> None:
    model = FakeModel("never")
    agent = AgentChat(
        settings, model=model, ledger=FakeLedger() if ledger else None, has_key=lambda: key
    )
    assert await run(agent) == [("error", {"code": code})]
    assert model.calls == []  # 모델을 부르지 않는다


async def test_a_budget_limit_or_kill_switch_stops_the_call_before_the_model() -> None:
    model = FakeModel("never")
    events = await run(chat(model, FakeLedger(refuse="LLM_DISABLED")))
    assert events[-1] == ("error", {"code": "LLM_DISABLED"})
    assert model.calls == []


async def test_a_model_failure_is_reported_by_code_only() -> None:
    events = await run(chat(FakeModel(fail=AgentUnavailable("LLM_AUTH"))))
    assert events[-1] == ("error", {"code": "LLM_AUTH"})


async def test_an_empty_model_answer_is_an_error() -> None:
    assert (await run(chat(FakeModel(""))))[-1] == ("error", {"code": "EMPTY_ANSWER"})


# ---- 비용 장부 ----


async def test_the_ledger_reserves_the_worst_case_then_settles_to_the_real_cost() -> None:
    ledger = FakeLedger()
    await run(chat(FakeModel("답"), ledger))
    ((run_id, worst),) = ledger.reserved
    ((settled_run, reserved, actual),) = ledger.settled
    assert run_id == settled_run == "agent-chat" and reserved == worst
    assert (
        actual == round(120 * 2.0 + 40 * 10.0) and actual < worst
    )  # 실제 비용이 최악 예약보다 작다


async def test_translation_is_also_counted() -> None:
    ledger = FakeLedger()
    await run(chat(FakeModel("답", "回答"), ledger), caption="ja")
    assert len(ledger.reserved) == 2 and len(ledger.settled) == 2


# ---- HTTP 경로 ----


@pytest.fixture
def client():
    holder = {}

    def use(agent: AgentChat) -> TestClient:
        holder["agent"] = agent
        app.dependency_overrides[get_agent_chat] = lambda: agent
        return TestClient(app)

    yield use
    app.dependency_overrides.pop(get_agent_chat, None)


def test_the_route_streams_events(client) -> None:
    response = client(chat(FakeModel("답입니다."))).post(
        "/agent/ask",
        json={"question": "지금 무슨 단계야?", "context": "stage: analysis", "lang": "ko"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert (
        response.headers["cache-control"] == "no-cache"
        and response.headers["x-accel-buffering"] == "no"
    )
    assert [name for name, _ in parse(response.text)] == ["status", "status", "answer", "done"]


def test_the_route_refuses_an_empty_question_and_unknown_fields(client) -> None:
    api = client(chat(FakeModel("never")))
    assert api.post("/agent/ask", json={"question": "  "}).json() == {"detail": "empty_question"}
    assert api.post("/agent/ask", json={"question": "q", "token": "x"}).status_code == 422


def test_the_route_reports_an_unconfigured_agent_as_an_event_not_a_crash(client) -> None:
    api = client(AgentChat(AgentSettings(enabled=False), model=FakeModel(), ledger=None))
    response = api.post("/agent/ask", json={"question": "q"})
    assert response.status_code == 200 and parse(response.text) == [
        ("error", {"code": "AGENT_DISABLED"})
    ]


def test_the_default_agent_is_off_until_the_operator_turns_it_on() -> None:
    from cmp_api.config import Settings

    assert Settings().agent_chat_enabled is False and Settings().agent_model == "claude-haiku-5-5"


def test_the_public_edge_does_not_route_the_agent_endpoint() -> None:
    caddy = (ROOT / "infra" / "demo" / "agent-routes.caddy").read_text(encoding="utf-8")
    paths = [
        line for line in caddy.splitlines() if line.lstrip().startswith("@") and "path" in line
    ]
    assert paths and not any("/agent/" in line or "/agent " in line for line in paths)


def test_compose_gives_the_api_the_agent_settings_and_the_shared_ledger() -> None:
    import yaml

    compose = yaml.safe_load((ROOT / "infra" / "demo" / "compose.yaml").read_text(encoding="utf-8"))
    api = compose["services"]["api"]
    env = api["environment"]
    assert env["AGENT_CHAT_ENABLED"] == "${AGENT_CHAT_ENABLED:-false}"  # off by default
    assert env["LLM_LEDGER_PATH"] == "/opt/cmp/llm/ledger.json"  # the worker's ledger file
    assert env["LLM_KILL_SWITCH_PATH"] == "/opt/cmp/llm/disabled"
    assert "ANTHROPIC_API_KEY" in env
    assert "/opt/cmp/data/llm:/opt/cmp/llm" in api["volumes"]
    worker_volumes = compose["services"]["worker"]["volumes"]
    assert "/opt/cmp/data/llm:/opt/cmp/llm" in worker_volumes  # same host folder: one budget
