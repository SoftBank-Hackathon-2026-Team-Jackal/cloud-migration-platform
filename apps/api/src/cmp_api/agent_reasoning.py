"""자칼의 판단 루프: 지금 지표를 도구로 읽고, 필요한 skill을 읽은 뒤, 판단 근거(관찰 → 판단 → 권고)와 답을 함께 낸다.

단발 호출(`agent_chat.AgentChat`의 기본 경로)은 웹이 보낸 상태 요약만 본다. 이 루프는 그 위에 세 가지를 더한다.
- `read_traffic_health`: 중계 서버가 잰 원본·대상의 500 오류율과 p99(`/system/traffic-health`와 같은 값)를 직접 읽는다.
  상태 요약에 적힌 숫자나 모델의 기억으로 대신하지 않게 하려는 것이다.
- `load_skill`: `skills/ops-*`의 운영 지침(지표 읽는 법, 롤백·확정 판단, 단계 설명)을 필요할 때만 읽는다.
- `submit_answer`: 판단 근거 1~4개와 음성 답을 정해진 모양으로 낸다. 근거는 화면에 보이는 요약이다(모델 내부 사고를
  그대로 옮긴 것이 아니라, 모델이 답의 근거로 내놓은 관찰·판단·권고다).

도구는 읽기 전용이다. 롤백·확정·장애 스위치 같은 실행 도구는 주지 않는다(실행은 사람이 화면에서 한다).
모델 호출은 한 번마다 비용 장부에 최악 비용을 예약하고 실제 비용으로 맞춘다(단발 경로와 같은 장부·긴급 중지).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from cmp_agents.llm import LLMClient, LLMError, LLMResponse, Message, ToolCall, ToolDefinition
from cmp_agents.runtime import AgentRun, Budget, run_agent
from cmp_agents.skills import SkillLoader, make_load_skill_tool
from cmp_agents.tools import ToolRegistry, ToolSpec
from pydantic import BaseModel, ConfigDict, Field

# 자칼이 읽을 수 있는 skill과 한 줄 설명(시스템 지침의 목록). 이전 단계용 skill(network 등)은 주지 않는다.
JACKAL_SKILLS: dict[str, str] = {
    "ops-traffic-health": "원본·대상의 500 오류율과 p99를 읽고 이상인지 판단하는 법",
    "ops-rollback": "트래픽 전환 뒤 롤백·확정을 권할지 판단하는 법과 단계별로 할 수 있는 일",
    "ops-migration-stages": "이전 단계의 순서와 뜻, 지금 누가 무엇을 해야 넘어가는지",
}
SUBMIT = "submit_answer"
HEALTH_TOOL = "read_traffic_health"
MAX_OUTPUT_TOKENS = 800
RECENT_POINTS = 6
LANGUAGE = {"ko": "한국어", "en": "English", "ja": "日本語"}


class Thought(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["observe", "judge", "recommend"]
    text: str = Field(min_length=1, max_length=200)


class JackalAnswer(BaseModel):
    """`submit_answer`의 입력. 근거는 화면에, 답은 음성으로 간다."""

    model_config = ConfigDict(extra="forbid")

    reasoning: list[Thought] = Field(min_length=1, max_length=4)
    answer: str = Field(min_length=1, max_length=400)


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def health_digest(view: dict[str, Any]) -> dict[str, Any]:
    """`HealthStore.view()`에서 모델에 줄 부분만: 신선도, 30초 요약, 최근 몇 구간의 측면별 값."""
    recent = []
    if view.get("fresh"):
        for point in (view.get("series") or [])[-RECENT_POINTS:]:
            recent.append(
                {
                    side: {
                        key: (point.get(side) or {}).get(key)
                        for key in ("requests", "error_rate", "p99_ms")
                    }
                    for side in ("origin", "target")
                }
            )
    return {
        "fresh": bool(view.get("fresh")),
        "age_seconds": view.get("age_seconds"),
        "summary_last_30s": view.get("summary"),
        "recent_windows_5s": recent,
        "thresholds": {
            "target_error_rate_alert": 0.05,
            "p99_limit_ms": "max(origin p99 x 1.5, 200)",
            "min_requests": 30,
        },
    }


def make_health_tool(read: Callable[[], dict[str, Any]]) -> ToolSpec:
    def handler(_: _NoArgs) -> str:
        return json.dumps(health_digest(read()), ensure_ascii=False)

    return ToolSpec(
        name=HEALTH_TOOL,
        description=(
            "Read the live 5xx error rate and p99 latency of the origin (AWS) and the target, "
            "measured at the relay Envoy. fresh=false means the numbers are unknown right now."
        ),
        input_model=_NoArgs,
        handler=handler,
    )


def reasoning_prompt(lang: str, screen_lang: str) -> str:
    skills = "\n".join(f"- {name}: {about}" for name, about in JACKAL_SKILLS.items())
    return "\n".join(
        [
            '너는 클라우드 마이그레이션 플랫폼의 음성 안내 에이전트 "자칼"이다.',
            '사용자 메시지의 "현재 상태"와 도구 결과에 있는 사실만 근거로 답한다. 없는 것은 추측하지 말고 모른다고 말한다.',
            "",
            "## 일하는 순서",
            f"1. 서비스 상태·오류·지연·롤백·확정을 묻거나 대상에 이상이 의심되면 먼저 `{HEALTH_TOOL}`로 지금 값을 읽는다.",
            "2. 아래 skill 중 질문에 맞는 것이 있으면 `load_skill`로 읽고 그 기준을 따른다. 맞는 것이 없으면 읽지 않는다.",
            f"3. `{SUBMIT}`로 끝낸다. 도구는 한 번에 여러 개 불러도 된다. 일반 글로 답하지 않는다.",
            "",
            "## skill",
            skills,
            "",
            f"## `{SUBMIT}` 작성법",
            "- reasoning: 1~4개. observe(읽은 사실과 숫자) → judge(기준과 비교한 판단) → recommend(권고) 순서로, 각 한 문장.",
            f"  화면에 보이는 글이므로 {LANGUAGE[screen_lang]}로 쓴다. 숫자는 도구 결과의 값을 그대로 쓴다.",
            f"- answer: 말로 읽어 줄 답. {LANGUAGE[lang]}로 1~3문장, 목록·마크다운·이모지 없이 짧고 쉬운 말로 쓴다.",
            "- 롤백·확정은 사람이 화면에서 한다. 네가 실행했다고 말하지 않는다.",
        ]
    )


class Ledger(Protocol):
    def reserve(self, run: str, worst_case_micro: int) -> int: ...

    def settle(self, run: str, reserved_micro: int, actual_micro: int) -> None: ...


@dataclass
class MeteredLLM:
    """모델 호출 한 번마다 장부에 최악 비용을 예약하고 실제 비용으로 맞춘다. 한도·긴급 중지면 부르지 않는다."""

    inner: LLMClient
    ledger: Ledger
    run_id: str
    price_input: float
    price_output: float
    max_output_tokens: int = MAX_OUTPUT_TOKENS

    def _micro(self, tokens_in: int, tokens_out: int) -> int:
        return round(tokens_in * self.price_input + tokens_out * self.price_output)

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> LLMResponse:
        chars = len(system) + sum(
            len(m.content)
            + len(json.dumps([c.arguments for c in m.tool_calls], ensure_ascii=False))
            for m in messages
        )
        chars += len(json.dumps([t.parameters for t in tools], ensure_ascii=False))
        worst = self._micro(int(chars / 2.5) + 512, self.max_output_tokens)
        try:
            reserved = self.ledger.reserve(self.run_id, worst)
        except Exception as exc:  # noqa: BLE001 - LLMBudgetExceeded 등: 코드만 꺼낸다
            raise LLMError(getattr(exc, "code", None) or "AGENT_BUDGET") from None
        try:
            response = self.inner.complete(system=system, messages=messages, tools=tools)
        except BaseException:
            self.ledger.settle(self.run_id, reserved, 0)
            raise
        self.ledger.settle(
            self.run_id, reserved, self._micro(response.input_tokens, response.output_tokens)
        )
        return response


class AnthropicToolChat:
    """도구를 쓰는 Anthropic 호출(`LLMClient`). SDK는 처음 부를 때 가져온다.

    `cmp_agents.anthropic_client.AnthropicLLMClient`는 Sonnet용 `effort`·캐시 설정을 보내므로, 작은 모델에도 맞는
    최소 요청만 보내는 이 클라이언트를 따로 둔다. 메시지·도구 변환은 같은 함수를 쓴다.
    """

    def __init__(self, model: str, timeout: float = 30.0) -> None:
        self.model, self.timeout = model, timeout

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> LLMResponse:
        import anthropic
        from cmp_agents.anthropic_client import to_api_messages, to_api_tools

        client = anthropic.Anthropic(timeout=self.timeout)
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                tools=to_api_tools(tools),
                messages=to_api_messages(messages),
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise LLMError("LLM_AUTH") from None
        except (anthropic.BadRequestError, anthropic.NotFoundError):
            raise LLMError("LLM_BAD_REQUEST") from None
        except anthropic.RateLimitError:
            raise LLMError("LLM_RATE_LIMIT") from None
        except anthropic.APIError:
            raise LLMError("LLM_UPSTREAM") from None
        if response.stop_reason == "max_tokens":
            raise LLMError("LLM_OUTPUT_TRUNCATED")
        return LLMResponse(
            tool_calls=[
                ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
                for b in response.content
                if b.type == "tool_use"
            ],
            text="".join(b.text for b in response.content if b.type == "text") or None,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


Notify = Callable[[str, dict[str, Any]], None]


@dataclass
class Reasoner:
    """한 질문에 대한 판단 루프. `notify(step, detail)`로 도구를 쓰기 직전에 진행 신호를 낸다."""

    llm: LLMClient
    skills_root: Path
    read_health: Callable[[], dict[str, Any]]
    max_turns: int = 4
    max_seconds: float = 40.0

    def run(
        self, question: str, context: str, lang: str, screen_lang: str, notify: Notify
    ) -> AgentRun:
        loader = SkillLoader(self.skills_root, allowed=list(JACKAL_SKILLS))
        skill_tool = make_load_skill_tool(loader)
        health_tool = make_health_tool(self.read_health)

        def load_skill(args: Any) -> Any:
            notify("skill", {"name": str(getattr(args, "name", ""))})
            return skill_tool.handler(args)

        def read_health(args: Any) -> Any:
            notify("tool", {"name": HEALTH_TOOL})
            return health_tool.handler(args)

        tools = ToolRegistry()
        tools.register(
            ToolSpec(skill_tool.name, skill_tool.description, skill_tool.input_model, load_skill)
        )
        tools.register(
            ToolSpec(
                health_tool.name, health_tool.description, health_tool.input_model, read_health
            )
        )
        task = f"현재 상태:\n{context or '(없음)'}\n\n질문: {question}"
        return run_agent(
            role="jackal",
            task=task,
            llm=self.llm,
            tools=tools,
            submit_name=SUBMIT,
            result_model=JackalAnswer,
            budget=Budget(
                max_turns=self.max_turns,
                max_input_tokens=60_000,
                max_output_tokens=4_000,
                max_seconds=self.max_seconds,
            ),
            skills=loader,
            prompt=reasoning_prompt(lang, screen_lang),
        )
