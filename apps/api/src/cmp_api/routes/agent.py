"""에이전트 채팅(`POST /agent/ask`): 질문 → 진행 신호 → 답을 SSE로.

웹 서버가 부른다(공개 엣지에는 이 경로가 없다). 요청: `{question, context, lang, captionLang?}`. 응답은 `text/event-stream`:
`status`(step: reading_state | asking_model | skill | tool | fallback | translating) → `answer`({answer, caption?, provider,
reasoning?, skills?}) → `done`,
쓸 수 없으면 `error`({code}) 하나. 질문이 비면 400. 모델 호출은 비용 장부와 긴급 중지를 거친다(`cmp_api.agent_chat`).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from cmp_api import traffic_health
from cmp_api.agent_chat import RUN_ID, AgentChat, AgentSettings, build_ledger, clean
from cmp_api.agent_reasoning import AnthropicToolChat, MeteredLLM, Reasoner
from cmp_api.config import settings

router = APIRouter(prefix="/agent", tags=["agent"])


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=2000)
    context: str = Field(default="", max_length=20000)
    lang: str = "ko"
    captionLang: str | None = None


@lru_cache(maxsize=1)
def _chat() -> AgentChat:
    config = AgentSettings(
        enabled=settings.agent_chat_enabled,
        model=settings.agent_model,
        caption_model=settings.agent_caption_model,
        ledger_path=settings.llm_ledger_path,
        kill_switch_path=settings.llm_kill_switch_path,
    )
    ledger = build_ledger(config) if config.enabled else None
    return AgentChat(
        config,
        ledger=ledger,
        has_key=lambda: bool(os.environ.get("ANTHROPIC_API_KEY")),
        reasoner=_reasoner(config, ledger) if settings.agent_reasoning else None,
    )


def _reasoner(config: AgentSettings, ledger) -> Reasoner | None:
    """판단 루프: 같은 장부로 호출마다 비용을 맞추고, 지표는 메모리의 `traffic_health.STORE`에서 읽는다."""
    if ledger is None:
        return None
    from cmp_providers.resources import resource_root

    return Reasoner(
        llm=MeteredLLM(
            AnthropicToolChat(config.model),
            ledger,
            RUN_ID,
            config.price_input,
            config.price_output,
        ),
        skills_root=resource_root() / "skills",
        read_health=traffic_health.STORE.view,
        max_turns=settings.agent_reasoning_turns,
    )


def get_agent_chat() -> AgentChat:
    return _chat()


ChatDep = Annotated[AgentChat, Depends(get_agent_chat)]


@router.post(
    "/ask",
    responses={
        200: {
            "description": "SSE: status*, answer, done | error",
            "content": {"text/event-stream": {}},
        },
        400: {"description": "empty_question"},
    },
)
async def ask(body: AskBody, chat: ChatDep):
    try:
        request = clean(body.question, body.context, body.lang, body.captionLang)
    except ValueError:
        raise HTTPException(status_code=400, detail="empty_question") from None
    return StreamingResponse(
        chat.ask(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
