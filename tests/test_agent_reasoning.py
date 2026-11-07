"""자칼 판단 루프: 지표 도구·skill을 쓰고 판단 근거와 답을 함께 낸다. 모델과 장부는 가짜로 대신한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cmp_agents.llm import FakeLLMClient, LLMError, LLMResponse, call
from cmp_api.agent_chat import AgentChat, AgentSettings, clean
from cmp_api.agent_reasoning import (
    HEALTH_TOOL,
    JACKAL_SKILLS,
    SUBMIT,
    MeteredLLM,
    Reasoner,
    health_digest,
    reasoning_prompt,
)

ROOT = Path(__file__).resolve().parents[1]

BAD = {
    "at": "now",
    "fresh": True,
    "age_seconds": 2.0,
    "summary": {
        "origin": {"requests": 600, "failed": 0, "error_rate": 0.0, "p99_ms": 100, "rps": 20},
        "target": {"requests": 590, "failed": 284, "error_rate": 0.4814, "p99_ms": 1000, "rps": 20},
    },
    "series": [
        {
            "window_seconds": 5,
            "origin": {"requests": 100, "error_rate": 0.0, "p99_ms": 100, "failed": 0},
            "target": {"requests": 100, "error_rate": 0.5, "p99_ms": 1000, "failed": 50},
        }
    ]
    * 10,
}
SUBMITTED = {
    "reasoning": [
        {"kind": "observe", "text": "대상 오류율 48.1%, p99 1000ms. 원본은 0%, 100ms."},
        {"kind": "judge", "text": "기준 5%와 200ms를 모두 넘었고 원본은 정상이다."},
        {"kind": "recommend", "text": "확정하지 말고 롤백한다."},
    ],
    "answer": "대상 오류율이 48퍼센트로 기준을 크게 넘었습니다. 롤백을 권합니다.",
}


class FakeLedger:
    def __init__(self, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.reserved: list[int] = []
        self.settled: list[tuple[int, int]] = []

    def reserve(self, run: str, worst_case_micro: int) -> int:
        if self.refuse:
            exc = Exception(self.refuse)
            exc.code = self.refuse  # type: ignore[attr-defined]
            raise exc
        self.reserved.append(worst_case_micro)
        return worst_case_micro

    def settle(self, run: str, reserved_micro: int, actual_micro: int) -> None:
        self.settled.append((reserved_micro, actual_micro))


class FakeModel:
    """단발 경로(대신 답하기·자막)용."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, str]] = []

    def complete(self, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.calls.append((system, user, model))
        return self.answers.pop(0), 100, 20


def scripted(*responses: LLMResponse) -> FakeLLMClient:
    return FakeLLMClient(list(responses))


def reasoner(llm, health=lambda: BAD, ledger: FakeLedger | None = None) -> Reasoner:
    metered = MeteredLLM(llm, ledger or FakeLedger(), "agent-chat", 2.0, 10.0)
    return Reasoner(llm=metered, skills_root=ROOT / "skills", read_health=health)


def parse(text: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def ask(agent: AgentChat, question="롤백해야 해?", caption=None) -> list[tuple[str, dict]]:
    request = clean(question, "stage: traffic (stabilizing)", "ko", caption)
    return parse("".join([b async for b in agent.ask(request)]))


def agent(r: Reasoner, model: FakeModel | None = None) -> AgentChat:
    return AgentChat(
        AgentSettings(enabled=True),
        model=model or FakeModel(),
        ledger=FakeLedger(),
        has_key=lambda: True,
        reasoner=r,
    )


# ---- skill 파일 ----


@pytest.mark.parametrize("name", list(JACKAL_SKILLS))
def test_every_jackal_skill_has_guidance(name: str) -> None:
    text = (ROOT / "skills" / name / "SKILL.md").read_text()
    assert text.startswith(f"# {name}\n")
    assert "## 언제 쓰는지" in text


def test_the_prompt_lists_the_skills_and_the_tools() -> None:
    prompt = reasoning_prompt("ko", "ja")
    for name in JACKAL_SKILLS:
        assert name in prompt
    assert HEALTH_TOOL in prompt and SUBMIT in prompt
    assert "日本語로 쓴다" in prompt  # 근거는 화면 언어로


# ---- 지표 요약 ----


def test_the_health_digest_keeps_recent_windows_only_when_fresh() -> None:
    digest = health_digest(BAD)
    assert digest["fresh"] is True
    assert len(digest["recent_windows_5s"]) == 6
    assert digest["recent_windows_5s"][0]["target"] == {
        "requests": 100,
        "error_rate": 0.5,
        "p99_ms": 1000,
    }
    stale = health_digest({**BAD, "fresh": False, "summary": {"origin": None, "target": None}})
    assert stale["recent_windows_5s"] == []
    assert stale["summary_last_30s"] == {"origin": None, "target": None}


# ---- 판단 루프 ----


async def test_the_loop_reads_the_metrics_and_a_skill_then_answers_with_its_reasoning() -> None:
    llm = scripted(
        LLMResponse(
            tool_calls=[call(HEALTH_TOOL), call("load_skill", name="ops-rollback")],
            input_tokens=900,
            output_tokens=40,
        ),
        LLMResponse(tool_calls=[call(SUBMIT, **SUBMITTED)], input_tokens=2500, output_tokens=150),
    )
    events = await ask(agent(reasoner(llm)))
    steps = [d.get("step") for e, d in events if e == "status"]
    assert steps == ["reading_state", "asking_model", "tool", "skill"]
    assert {"step": "skill", "name": "ops-rollback"} in [d for e, d in events]
    answer = next(d for e, d in events if e == "answer")
    assert answer["answer"] == SUBMITTED["answer"]
    assert [t["kind"] for t in answer["reasoning"]] == ["observe", "judge", "recommend"]
    assert answer["skills"] == ["ops-rollback"]
    assert events[-1] == ("done", {})
    # 두 번째 호출에 지표와 skill 본문이 도구 결과로 들어갔다.
    results = [m.content for m in llm.calls[1].messages if m.role == "tool"]
    assert '"error_rate": 0.4814' in results[0]
    assert results[1].startswith("# ops-rollback")


async def test_a_skill_outside_the_jackal_list_is_refused() -> None:
    llm = scripted(
        LLMResponse(tool_calls=[call("load_skill", name="network")]),
        LLMResponse(tool_calls=[call(SUBMIT, **SUBMITTED)]),
    )
    events = await ask(agent(reasoner(llm)))
    assert next(d for e, d in events if e == "answer")["skills"] == []
    result = next(m.content for m in llm.calls[1].messages if m.role == "tool")
    assert result.startswith("error: skill 'network' is not available")


async def test_a_loop_that_never_submits_falls_back_to_a_single_answer() -> None:
    llm = scripted(*[LLMResponse(text="음...") for _ in range(4)])
    model = FakeModel("지금은 안정화 구간입니다.")
    events = await ask(agent(reasoner(llm), model))
    assert ("status", {"step": "fallback"}) in events
    answer = next(d for e, d in events if e == "answer")
    assert answer == {"answer": "지금은 안정화 구간입니다.", "provider": "platform"}
    assert len(model.calls) == 1


async def test_a_model_error_ends_with_its_code_and_no_second_call() -> None:
    class Broken:
        def complete(self, **_):
            raise LLMError("LLM_AUTH")

    model = FakeModel()
    events = await ask(agent(reasoner(Broken()), model))
    assert events == [
        ("status", {"step": "reading_state", "chars": 28}),
        ("status", {"step": "asking_model", "model": "claude-haiku-5-5"}),
        ("error", {"code": "LLM_AUTH"}),
    ]
    assert model.calls == []


async def test_a_budget_limit_stops_before_the_model() -> None:
    llm = scripted(LLMResponse(tool_calls=[call(SUBMIT, **SUBMITTED)]))
    events = await ask(agent(reasoner(llm, ledger=FakeLedger(refuse="LLM_BUDGET_DAY"))))
    assert events[-1] == ("error", {"code": "LLM_BUDGET_DAY"})
    assert llm.calls == []


async def test_each_model_call_is_reserved_then_settled() -> None:
    ledger = FakeLedger()
    llm = scripted(
        LLMResponse(tool_calls=[call(HEALTH_TOOL)], input_tokens=1000, output_tokens=50),
        LLMResponse(tool_calls=[call(SUBMIT, **SUBMITTED)], input_tokens=2000, output_tokens=100),
    )
    await ask(agent(reasoner(llm, ledger=ledger)))
    assert len(ledger.reserved) == 2
    assert [actual for _, actual in ledger.settled] == [1000 * 2 + 50 * 10, 2000 * 2 + 100 * 10]
    assert all(reserved >= actual for reserved, actual in ledger.settled)


async def test_the_spoken_answer_is_translated_for_the_caption() -> None:
    llm = scripted(LLMResponse(tool_calls=[call(SUBMIT, **SUBMITTED)]))
    model = FakeModel("ロールバックを勧めます。")
    events = await ask(agent(reasoner(llm), model), caption="ja")
    answer = next(d for e, d in events if e == "answer")
    assert answer["caption"] == "ロールバックを勧めます。"
    assert model.calls[0][1] == SUBMITTED["answer"]
    # 근거는 화면 언어(일본어)로 쓰라고 지시했다.
    assert "日本語로 쓴다" in llm.calls[0].system


def test_the_route_builds_the_reasoner_only_with_a_ledger(monkeypatch, tmp_path) -> None:
    from cmp_api.routes import agent as route

    monkeypatch.setattr(route.settings, "agent_chat_enabled", True)
    monkeypatch.setattr(route.settings, "llm_ledger_path", str(tmp_path / "ledger.json"))
    route._chat.cache_clear()
    try:
        chat = route._chat()
        assert chat.reasoner is not None
        assert (chat.reasoner.skills_root / "ops-rollback" / "SKILL.md").is_file()
        monkeypatch.setattr(route.settings, "agent_reasoning", False)
        route._chat.cache_clear()
        assert route._chat().reasoner is None
    finally:
        route._chat.cache_clear()
