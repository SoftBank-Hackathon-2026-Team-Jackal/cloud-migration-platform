"""서버 맥박. 웹 상단 위젯이 읽는다.

- `GET /system/pulse`: 플랫폼 서버의 CPU·메모리 사용률, 최근 5분 오류 수, 그리고 원본·이전 서버가 보낸 값(`source`·`target`).
  웹 서버만 부른다(공개 엣지에 없다). 퍼센트와 개수만 돌려주고, 읽을 수 없거나 15초 넘게 안 온 값은 null이다.
- `POST /system/pulse/report`: 원본·이전 서버의 작은 리포터가 3초마다 CPU·메모리를 보낸다. 공유 토큰(`X-Pulse-Token`)이 있어야 한다.
  토큰 설정(`PULSE_REPORT_TOKEN`)이 비어 있으면 수신 자체를 끈다(503). 값은 메모리에만 둔다.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from cmp_api import pulse
from cmp_api.config import settings
from cmp_api.routes.migrations import get_event_sessions

router = APIRouter(prefix="/system", tags=["system"])


class Side(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu_percent: float
    mem_percent: float | None
    mem_used_mb: int
    mem_total_mb: int
    nodes: int
    age_seconds: float


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
    source: Side | None
    target: Side | None


class PulseReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["source", "target"]
    node: str = Field(pattern=r"^[A-Za-z0-9._-]{1,40}$")
    cpu_percent: float = Field(ge=0, le=100)
    mem_used_mb: int = Field(ge=0, le=4_000_000)
    mem_total_mb: int = Field(gt=0, le=4_000_000)


def get_reports() -> pulse.ReportStore:
    return pulse.REPORTS


def get_report_token() -> str:
    return settings.pulse_report_token


@router.get("/pulse", response_model=Pulse, summary="Server CPU/memory use and recent errors")
async def get_pulse(
    sessions: Annotated[object, Depends(get_event_sessions)],
    reports: Annotated[pulse.ReportStore, Depends(get_reports)],
) -> dict:
    return await asyncio.to_thread(pulse.snapshot, sessions, reports=reports)


@router.post(
    "/pulse/report", status_code=204, summary="Source/target server reports its CPU and memory"
)
async def post_pulse_report(
    body: PulseReport,
    reports: Annotated[pulse.ReportStore, Depends(get_reports)],
    token: Annotated[str, Depends(get_report_token)],
    x_pulse_token: Annotated[str | None, Header()] = None,
) -> Response:
    if not token:
        raise HTTPException(status_code=503, detail="PULSE_REPORT_DISABLED")
    if not x_pulse_token or not hmac.compare_digest(x_pulse_token.encode(), token.encode()):
        raise HTTPException(status_code=403, detail="PULSE_TOKEN_INVALID")
    if body.mem_used_mb > body.mem_total_mb:
        raise HTTPException(status_code=422, detail="PULSE_MEMORY_INVALID")
    reports.put(body.role, body.node, body.cpu_percent, body.mem_used_mb, body.mem_total_mb)
    return Response(status_code=204)
