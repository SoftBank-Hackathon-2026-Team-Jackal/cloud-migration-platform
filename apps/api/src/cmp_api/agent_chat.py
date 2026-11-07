"""에이전트 "자칼"의 질문 답을 플랫폼 서버에서 만든다(웹 `/api/agent`와 같은 동작을 서버로 옮긴 것).

웹이 보낸 "현재 상태 요약"만 근거로 답하고(상태에 없는 것은 모른다고 한다), 답 만드는 과정을 사람이 볼 수 있는
진행 신호(`status`)로 먼저 내보낸다: "상태 읽는 중 → 모델에 묻는 중 → 자막 옮기는 중". 신호는 실제로 하는 일만 낸다.

- 모델 호출은 비용 장부(ADR-004 `CostLedger`)를 거친다: 이전·하루·한 달 한도와 긴급 중지(`disabled` 파일)가 그대로 적용된다.
  호출 전에 최악 비용을 예약하고 끝나면 실제 비용으로 맞춘다. 한도에 걸리면 모델을 부르기 전에 거절한다.
- 키는 SDK가 환경변수(ANTHROPIC_API_KEY)에서 읽는다. 이 모듈은 키를 읽지도 돌려주지도 않는다.
- 실패하면 오류 코드만 내보낸다. 웹은 그때 예전 경로(웹 서버가 직접 묻기) 또는 규칙 기반 답으로 대신한다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

MAX_QUESTION = 300
MAX_CONTEXT = 6000
MAX_OUTPUT_TOKENS = 300
LANGUAGE = {"ko": "한국어", "en": "English", "ja": "日本語"}
RUN_ID = (
    "agent-chat"  # 장부에서 에이전트 채팅의 이전별(run) 한도를 따로 세지 않도록 한 묶음으로 센다
)


class ModelClient(Protocol):
    """`complete(system, user, model) -> (text, input_tokens, output_tokens)`. 시험에서는 가짜로 대신한다."""

    def complete(self, system: str, user: str, model: str) -> tuple[str, int, int]: ...


class Ledger(Protocol):
    def reserve(self, run: str, worst_case_micro: int) -> int: ...

    def settle(self, run: str, reserved_micro: int, actual_micro: int) -> None: ...


class AgentUnavailable(Exception):
    """에이전트를 쓸 수 없다. `code`는 화면에 보여도 되는 안정된 값."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = False
    model: str = "claude-haiku-5-5"
    caption_model: str | None = None
    ledger_path: str | None = None
    kill_switch_path: str | None = None
    # 요율 표에 없는 모델은 보수적으로(비싸게) 센다: 한도가 먼저 걸리는 쪽이 안전하다.
    price_input: float = 2.0  # $/백만 토큰
    price_output: float = 10.0
    limit_day_usd: float = 20.0
    limit_month_usd: float = 100.0


@dataclass
class AskRequest:
    question: str
    context: str
    lang: str = "ko"
    caption_lang: str | None = None


def clean(question: Any, context: Any, lang: Any, caption_lang: Any) -> AskRequest:
    """입력을 다듬는다. 질문이 비면 ValueError(웹 `/api/agent`의 400과 같다)."""
    text = str(question or "").strip()[:MAX_QUESTION]
    if not text:
        raise ValueError("empty_question")
    language = lang if lang in LANGUAGE else "ko"
    caption = caption_lang if caption_lang in LANGUAGE and caption_lang != language else None
    return AskRequest(text, str(context or "")[:MAX_CONTEXT], language, caption)


def system_prompt(lang: str) -> str:
    return "\n".join(
        [
            '너는 클라우드 마이그레이션 플랫폼의 음성 안내 에이전트 "자칼"이다.',
            '아래 "현재 상태"에 적힌 사실만 근거로 사용자의 질문에 답한다. 상태에 없는 것은 추측하지 말고 모른다고 말한다.',
            f"답변 언어: {LANGUAGE[lang]}. 말로 읽어 줄 것이므로 1~3문장, 목록·마크다운·이모지 없이 짧고 쉬운 말로 쓴다.",
            '"로그를 읊어 줘"라고 하면 "최근 기록"을 순서대로 자연스러운 문장으로 읽어 준다.',
            "문제를 물으면 실패·사람 검토 대기·이미지 Digest 불일치·허용을 넘은 실패 요청 같은 것만 짚고, 없으면 없다고 말한다.",
        ]
    )


def caption_prompt(source: str, target: str) -> str:
    return "\n".join(
        [
            f"다음 {LANGUAGE[source]} 문장을 화면 자막으로 쓸 {LANGUAGE[target]}로 옮긴다.",
            "뜻을 더하거나 빼지 않는다. 숫자·단위·제품명·코드(예: GKE, PgBouncer, Digest)는 그대로 둔다.",
            "번역문만 출력한다. 설명·따옴표·마크다운은 쓰지 않는다.",
        ]
    )


def block(event: str, payload: dict[str, Any]) -> str:
    """SSE 블록 하나(`event:` + `data:`). `event_stream._block`과 같은 모양."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def cost_micro(
    price_input: float, price_output: float, input_tokens: int, output_tokens: int
) -> int:
    return round(
        input_tokens * price_input + output_tokens * price_output
    )  # 토큰 × $/백만 토큰 = 마이크로달러


class AnthropicChat:
    """Anthropic SDK 호출. SDK는 처음 부를 때 가져온다(시험·SDK 없는 환경에서 모듈을 읽을 수 있게)."""

    def __init__(self, timeout: float = 45.0) -> None:
        self.timeout = timeout

    def complete(self, system: str, user: str, model: str) -> tuple[str, int, int]:
        import anthropic

        client = anthropic.Anthropic(timeout=self.timeout)
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise AgentUnavailable("LLM_AUTH") from None
        except (anthropic.BadRequestError, anthropic.NotFoundError):
            raise AgentUnavailable("LLM_BAD_REQUEST") from None
        except anthropic.RateLimitError:
            raise AgentUnavailable("LLM_RATE_LIMIT") from None
        except anthropic.APIError:
            raise AgentUnavailable("LLM_UPSTREAM") from None
        text = "".join(
            part.text for part in response.content if getattr(part, "type", "") == "text"
        )
        return text.strip(), response.usage.input_tokens, response.usage.output_tokens


def build_ledger(settings: AgentSettings) -> Ledger | None:
    """장부 경로가 있으면 `CostLedger`(파일 잠금 공유). 없으면 None(그러면 에이전트를 쓰지 않는다)."""
    if not settings.ledger_path:
        return None
    from cmp_agents.cost import CostLedger, Limits

    return CostLedger(
        settings.ledger_path,
        Limits(per_day_usd=settings.limit_day_usd, per_month_usd=settings.limit_month_usd),
        kill_switch=settings.kill_switch_path,
    )


class AgentChat:
    def __init__(
        self,
        settings: AgentSettings,
        *,
        model: ModelClient | None = None,
        ledger: Ledger | None = None,
        has_key: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.model = model or AnthropicChat()
        self.ledger = ledger
        self._has_key = has_key or (lambda: True)

    def ready(self) -> str | None:
        """쓸 수 없는 이유 코드, 쓸 수 있으면 None."""
        if not self.settings.enabled:
            return "AGENT_DISABLED"
        if not self._has_key():
            return "AGENT_NO_KEY"
        if self.ledger is None:
            return "AGENT_NO_LEDGER"
        return None

    def _call(self, system: str, user: str, model: str) -> str:
        """장부에 최악 비용을 예약하고 모델을 부른 뒤 실제 비용으로 맞춘다. 한도에 걸리면 부르지 않는다."""
        assert self.ledger is not None
        s = self.settings
        worst = cost_micro(
            s.price_input,
            s.price_output,
            int((len(system) + len(user)) / 2.5) + 64,
            MAX_OUTPUT_TOKENS,
        )
        try:
            reserved = self.ledger.reserve(RUN_ID, worst)
        except Exception as exc:  # noqa: BLE001 - LLMBudgetExceeded 등: 코드만 꺼낸다
            code = getattr(exc, "code", None) or "AGENT_BUDGET"
            raise AgentUnavailable(code) from None
        text, tokens_in, tokens_out = self.model.complete(system, user, model)
        self.ledger.settle(
            RUN_ID, reserved, cost_micro(s.price_input, s.price_output, tokens_in, tokens_out)
        )
        return text

    async def ask(self, request: AskRequest) -> AsyncIterator[str]:
        """진행 신호 → 답 → 끝, 를 SSE 블록으로 낸다. 실패는 `error` 블록 하나로 끝낸다."""
        reason = self.ready()
        if reason:
            yield block("error", {"code": reason})
            return
        yield block("status", {"step": "reading_state", "chars": len(request.context)})
        user = f"현재 상태:\n{request.context or '(없음)'}\n\n질문: {request.question}"
        yield block("status", {"step": "asking_model", "model": self.settings.model})
        try:
            answer = await asyncio.to_thread(
                self._call, system_prompt(request.lang), user, self.settings.model
            )
        except AgentUnavailable as exc:
            yield block("error", {"code": exc.code})
            return
        if not answer:
            yield block("error", {"code": "EMPTY_ANSWER"})
            return
        payload: dict[str, Any] = {"answer": answer, "provider": "platform"}
        if request.caption_lang:
            yield block("status", {"step": "translating", "to": request.caption_lang})
            try:
                caption = await asyncio.to_thread(
                    self._call,
                    caption_prompt(request.lang, request.caption_lang),
                    answer,
                    self.settings.caption_model or self.settings.model,
                )
            except AgentUnavailable:
                caption = ""  # 자막만 빠지고 답은 그대로 간다
            if caption:
                payload["caption"] = caption
        yield block("answer", payload)
        yield block("done", {})
