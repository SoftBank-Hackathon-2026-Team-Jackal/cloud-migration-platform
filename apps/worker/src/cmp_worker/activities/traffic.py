"""트래픽 전환 Activity(#63). 프록시(Envoy) 호출 같은 외부 I/O는 여기서만 한다.

중계 서버 위치는 환경변수로 받는다(비밀값 아님). 설정이 없으면 전환하는 척하지 않고 실패한다.
  TRAFFIC_RDS_PATH     Envoy와 공유하는 라우트 파일 경로
  TRAFFIC_ENVOY_ADMIN  Envoy 관리 주소(기본 http://127.0.0.1:9901)
  TRAFFIC_TARGET_HOST  대상이 Host 헤더로 서비스를 구분할 때 덮어쓸 호스트(선택)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

from cmp_traffic import Handover, Sample, TrafficPolicy, check_handover, weights_for
from cmp_traffic.envoy import EnvoyObserver, EnvoyProxy
from temporalio import activity
from temporalio.exceptions import ApplicationError


def handover_target(handover: Handover) -> tuple[str, int] | None:
    """핸드오버의 외부 주소를 (호스트, 포트)로. 없거나 읽을 수 없으면 None."""
    parsed = urlparse(handover.external_endpoint or "")
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or handover.port


def build_ports() -> tuple[EnvoyProxy, EnvoyObserver]:
    rds = os.environ.get("TRAFFIC_RDS_PATH")
    if not rds:
        raise ApplicationError(
            "traffic relay is not configured (TRAFFIC_RDS_PATH)",
            type="RelayNotConfigured",
            non_retryable=True,
        )
    admin = os.environ.get("TRAFFIC_ENVOY_ADMIN", "http://127.0.0.1:9901")
    proxy = EnvoyProxy(Path(rds), admin, target_host=os.environ.get("TRAFFIC_TARGET_HOST") or None)
    return proxy, EnvoyObserver(admin)


@activity.defn
async def traffic_prepare(payload: dict) -> dict:
    """인계값을 확인하고 원본 기준선을 잰 뒤 원본 100% 설정을 깐다."""
    handover = Handover.model_validate(payload["handover"])
    check = check_handover(handover)
    if not check.ok:
        raise ApplicationError(check.reason, type="HandoverNotReady", non_retryable=True)
    policy = TrafficPolicy.model_validate(payload.get("policy") or {})
    proxy, observer = build_ports()
    # 전환 전에 이 중계 서버의 대상이 인계값의 목적지와 같은지 본다(다르면 엉뚱한 곳으로 보내지 않는다).
    wanted = handover_target(handover)
    actual = await asyncio.to_thread(observer.target_address)
    if wanted is None or actual != wanted:
        raise ApplicationError(
            "relay target does not match the handover",
            type="TargetMismatch",
            non_retryable=True,
        )
    baseline = await asyncio.to_thread(observer.sample_origin, policy.observe_seconds)
    await asyncio.to_thread(proxy.apply, weights_for(0))
    return {"baseline": baseline.model_dump()}


@activity.defn
async def traffic_set_weight(payload: dict) -> dict:
    """가중치를 적용하고 Envoy가 받아들였다고 확인될 때만 돌아온다."""
    proxy, _ = build_ports()
    weights = payload["weights"]
    try:
        await asyncio.to_thread(proxy.apply, weights)
    except RuntimeError as exc:
        raise ApplicationError(str(exc), type="ApplyNotConfirmed", non_retryable=True) from exc
    return {"weights": weights}


@activity.defn
async def traffic_observe(payload: dict) -> dict:
    """대상 쪽 한 관찰 창의 요청·실패·p99를 돌려준다."""
    _, observer = build_ports()
    sample: Sample = await asyncio.to_thread(observer.sample_target, payload["seconds"])
    return sample.model_dump()


@activity.defn
async def traffic_rollback(payload: dict) -> dict:
    """원본 100%로 되돌린다. 멱등이라 재시도해도 안전하다."""
    proxy, _ = build_ports()
    weights = weights_for(0)
    await asyncio.to_thread(proxy.apply, weights)
    return {"weights": weights}
