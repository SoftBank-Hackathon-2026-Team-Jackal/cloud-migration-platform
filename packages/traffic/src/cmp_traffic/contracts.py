"""트래픽 전환 입출력 계약(#61). 외부 I/O 없이 검증할 수 있는 순수 모델이다."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TrafficPolicy(BaseModel):
    """단계와 게이트 기준. 기본값은 예선 PoC 실험에서 쓴 보수적 값이다."""

    steps: list[int] = Field(default_factory=lambda: [10, 50, 100])
    observe_seconds: float = Field(default=120, gt=0)
    max_step_seconds: float = Field(default=600, gt=0)
    max_failed_requests: int = Field(default=0, ge=0)
    max_p99_ratio: float = Field(default=1.5, gt=0)
    # 원본 p99가 아주 작을 때(또는 WAN 지연이 더해질 때) 비율만으로 롤백하지 않도록 두는 절대 하한(ms).
    # 실제 WAN 시험에서 대상 p99가 73~97ms였기 때문에 기본값을 200으로 둔다.
    p99_floor_ms: float = Field(default=200, ge=0)
    # p99 초과가 연속으로 이 횟수만큼 관측 창에서 나와야 롤백한다(한 번의 이상치로 되돌리지 않는다).
    p99_breach_windows: int = Field(default=2, ge=1)
    # 이보다 적은 요청으로는 판단하지 않는다(모르면 안전한 쪽: 올리지 않는다).
    min_requests: int = Field(default=30, ge=1)
    # 마지막(100%) 단계 전에 "DB 쓰기 주체가 대상이고 역방향 복제가 정상"이 확인돼야 하는지.
    require_db_gate: bool = False
    # 100% 전환 뒤 최종 확정 전 안정화 구간(초). 0이면 예전처럼 100%에 닿자마자 끝난다.
    # 이 구간에서는 자동으로 되돌리지 않는다: 이상을 알리고(`traffic.alert`) 사람의 롤백·확정을 기다린다.
    # 아무 결정이 없으면 구간이 끝날 때 확정한다.
    stabilize_seconds: float = Field(default=0, ge=0, le=86_400)
    # 안정화 구간의 관찰 창 길이(초). 짧을수록 이상을 빨리 알린다.
    stabilize_window_seconds: float = Field(default=10, gt=0, le=600)
    # 안정화 구간에서 이 오류율(실패/요청)을 넘으면 알린다. p99는 전환 단계와 같은 기준(`p99_limit`)을 쓴다.
    alert_error_rate: float = Field(default=0.05, ge=0, le=1)
    # 안정화 구간에서 롤백하면 DB 쓰기 주체도 원본으로 되돌린다(데이터 이전 `rollback` 신호, 역방향 복제).
    # 다시 전환할 때는 DB를 먼저 대상으로 전환(`switchover`)하고 DB 게이트가 열린 뒤 트래픽을 옮긴다.
    rollback_db: bool = True
    # 원본·대상 모두 최소 요청 수에 못 미치면(= 사용자 트래픽이 없으면) 롤백하지 않고, 중계 서버에서 대상으로
    # 합성 요청을 보내 실패·p99를 확인한 뒤 진행한다. 끄면(False) 예전처럼 요청 부족은 보류 후 롤백이다.
    idle_probe: bool = True
    # 합성 요청 하나의 응답 제한(초). 넘으면 실패로 센다.
    idle_probe_timeout_s: float = Field(default=3.0, gt=0, le=30)

    @model_validator(mode="after")
    def _check_steps(self) -> TrafficPolicy:
        if not self.steps or self.steps != sorted(set(self.steps)):
            raise ValueError("steps는 오름차순이며 중복이 없어야 합니다.")
        if self.steps[0] <= 0 or self.steps[-1] != 100:
            raise ValueError("steps는 0보다 크게 시작해 100으로 끝나야 합니다.")
        return self


class TrafficShiftInput(BaseModel):
    """마이그레이션 입력의 `traffic` 항목(운영자가 주는 값, 비밀값 없음). 없으면 전환 단계는 scaffold로 남는다."""

    # Envoy가 있는 중계 서버 호스트의 워커 큐(예: relay-1). 없으면 기본 큐.
    relay_queue: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    policy: TrafficPolicy = Field(default_factory=TrafficPolicy)
    # 마지막(100%) 단계 전에 DB 쓰기 주체가 대상이고 역방향 복제가 정상인지(Data Migration이 알려 준다).
    db_gate_ok: bool | None = None


class Handover(BaseModel):
    """Provisioning이 넘기는 값 중 트래픽 전환에 필요한 것만 담는다."""

    status: Literal["READY", "FAILED", "UNKNOWN"]
    ready: bool
    external_endpoint: str | None = None
    health_path: str = "/health"
    port: int = Field(default=80, ge=1, le=65535)

    @field_validator("external_endpoint")
    @classmethod
    def _no_embedded_credentials(cls, value: str | None) -> str | None:
        if value and "@" in value:
            raise ValueError("endpoint에는 인증 정보를 넣을 수 없습니다.")
        return value


class Precheck(BaseModel):
    ok: bool
    reason: str = ""


class Sample(BaseModel):
    requests: int = Field(ge=0)
    failed: int = Field(ge=0)
    p99_ms: float = Field(ge=0)


class StepOutcome(BaseModel):
    step: int
    weights: dict[str, int]
    decision: Literal["advance", "hold", "rollback", "done"]
    reason: str = ""
    # 판정 근거의 기계가 읽는 값(웹이 말을 고르고 숫자를 채운다): kind=failed|p99|requests, 관련 수치.
    detail: dict[str, Any] = Field(default_factory=dict)


class ShiftResult(BaseModel):
    status: Literal["DONE", "ROLLED_BACK", "BLOCKED", "FAILED"]
    final_weights: dict[str, int]
    events: list[StepOutcome]
