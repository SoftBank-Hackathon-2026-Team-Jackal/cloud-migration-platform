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

from cmp_api import fault_control, pulse, traffic_health
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


class SideWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: int = Field(ge=0, le=10_000_000)
    errors_5xx: int = Field(ge=0, le=10_000_000)
    timeouts: int = Field(ge=0, le=10_000_000)
    failed: int = Field(ge=0, le=10_000_000)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    rps: float = Field(ge=0)
    p99_ms: float | None = Field(default=None, ge=0)


class FaultObserved(BaseModel):
    """리포터가 대상 앱 `/__cmp/fault`에서 읽은 실제 상태. active가 None이면 읽지 못했다."""

    model_config = ConfigDict(extra="forbid")

    active: bool | None
    error_rate: float | None = Field(default=None, ge=0, le=1)
    delay_ms: int | None = Field(default=None, ge=0, le=60_000)
    remaining_s: int | None = Field(default=None, ge=0, le=86_400)
    applied_id: str | None = Field(default=None, max_length=64)
    error: str | None = Field(default=None, max_length=200)


class HealthReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_seconds: float = Field(gt=0, le=600)
    origin: SideWindow
    target: SideWindow
    fault: FaultObserved | None = None


class SideSummary(BaseModel):
    requests: int
    failed: int
    error_rate: float | None
    p99_ms: float | None
    rps: float | None


class TrafficHealth(BaseModel):
    at: str
    fresh: bool
    age_seconds: float | None
    summary: dict[Literal["origin", "target"], SideSummary]
    series: list[dict]


def get_health_store() -> traffic_health.HealthStore:
    return traffic_health.STORE


def _check_token(token: str, given: str | None) -> None:
    if not token:
        raise HTTPException(status_code=503, detail="PULSE_REPORT_DISABLED")
    if not given or not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(status_code=403, detail="PULSE_TOKEN_INVALID")


@router.get(
    "/traffic-health",
    response_model=TrafficHealth,
    summary="Origin/target 5xx rate and p99 measured at the relay Envoy",
)
async def get_traffic_health(
    store: Annotated[traffic_health.HealthStore, Depends(get_health_store)],
) -> dict:
    return store.view()


def get_fault_control() -> fault_control.FaultControl:
    return fault_control.CONTROL


def get_tools_enabled() -> bool:
    return settings.demo_tools_enabled


class ReportAnswer(BaseModel):
    # 리포터가 대상 앱에 맞출 장애 명령(Tools 화면이 꺼져 있거나 명령이 없으면 null)
    fault: dict | None


@router.post(
    "/traffic-health/report",
    response_model=ReportAnswer,
    summary="Relay reports one window of origin/target request metrics",
)
async def post_traffic_health(
    body: HealthReport,
    store: Annotated[traffic_health.HealthStore, Depends(get_health_store)],
    control: Annotated[fault_control.FaultControl, Depends(get_fault_control)],
    tools: Annotated[bool, Depends(get_tools_enabled)],
    token: Annotated[str, Depends(get_report_token)],
    x_pulse_token: Annotated[str | None, Header()] = None,
) -> dict:
    _check_token(token, x_pulse_token)
    store.put(body.model_dump(exclude={"fault"}))
    if not tools:
        return {"fault": None}
    if body.fault is not None:
        control.observe(body.fault.model_dump())
    return {"fault": control.desired()}


class FaultSync(BaseModel):
    """리포터의 빠른 장애 동기화(1초 간격): 지표 없이 대상 앱의 실제 장애 상태만 싣는다."""

    model_config = ConfigDict(extra="forbid")

    fault: FaultObserved | None = None


@router.post(
    "/traffic-health/fault",
    response_model=ReportAnswer,
    summary="Relay syncs the demo fault switch (desired command out, observed state in)",
)
async def post_fault_sync(
    body: FaultSync,
    control: Annotated[fault_control.FaultControl, Depends(get_fault_control)],
    tools: Annotated[bool, Depends(get_tools_enabled)],
    token: Annotated[str, Depends(get_report_token)],
    x_pulse_token: Annotated[str | None, Header()] = None,
) -> dict:
    # 5초 지표 보고(/traffic-health/report)를 기다리지 않고 명령을 1초 안에 넘기고 적용 결과를 받는다.
    _check_token(token, x_pulse_token)
    if not tools:
        return {"fault": None}
    if body.fault is not None:
        control.observe(body.fault.model_dump())
    return {"fault": control.desired()}


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_rate: float = Field(default=0.5, ge=0, le=1)
    delay_ms: int = Field(default=800, ge=0, le=10_000)
    duration_s: int = Field(default=300, ge=1, le=900)


class FaultView(BaseModel):
    at: str
    desired: dict | None
    observed: dict | None
    observed_age_seconds: float | None
    applied: bool | None


def _tools_on(enabled: bool) -> None:
    if not enabled:
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/fault", response_model=FaultView, summary="Demo fault switch: desired vs observed")
async def get_fault(
    control: Annotated[fault_control.FaultControl, Depends(get_fault_control)],
    tools: Annotated[bool, Depends(get_tools_enabled)],
) -> dict:
    _tools_on(tools)
    return control.view()


@router.post("/fault", response_model=FaultView, summary="Demo fault switch: inject on the target")
async def post_fault(
    body: FaultRequest,
    control: Annotated[fault_control.FaultControl, Depends(get_fault_control)],
    tools: Annotated[bool, Depends(get_tools_enabled)],
) -> dict:
    _tools_on(tools)
    if body.error_rate == 0 and body.delay_ms == 0:
        raise HTTPException(status_code=422, detail="FAULT_EMPTY")
    control.turn_on(body.error_rate, body.delay_ms, body.duration_s)
    return control.view()


@router.delete("/fault", response_model=FaultView, summary="Demo fault switch: clear")
async def delete_fault(
    control: Annotated[fault_control.FaultControl, Depends(get_fault_control)],
    tools: Annotated[bool, Depends(get_tools_enabled)],
) -> dict:
    _tools_on(tools)
    control.turn_off()
    return control.view()
