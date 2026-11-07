"""`GET /system/pulse`: 플랫폼 서버의 CPU·메모리 사용률과 최근 5분 오류 수(웹 상단 위젯용).

웹 서버가 부른다(공개 엣지에는 이 경로가 없다). 퍼센트와 개수만 돌려주고, 읽을 수 없는 값은 null이다.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from cmp_api import pulse
from cmp_api.routes.migrations import get_event_sessions

router = APIRouter(prefix="/system", tags=["system"])


class Pulse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str
    cpu_percent: float | None
    mem_percent: float | None
    mem_used_mb: int | None
    mem_total_mb: int | None
    load1: float | None
    cores: int | None
    errors_5m: int | None
    window_seconds: int


@router.get("/pulse", response_model=Pulse, summary="Server CPU/memory use and recent errors")
async def get_pulse(sessions: Annotated[object, Depends(get_event_sessions)]) -> dict:
    return await asyncio.to_thread(pulse.snapshot, sessions)
