"""트래픽 전환 Activity(#63). 프록시(Envoy) 호출 같은 외부 I/O는 여기서만 한다.

중계 서버 위치는 환경변수로 받는다(비밀값 아님). 설정이 없으면 전환하는 척하지 않고 실패한다.
  TRAFFIC_RDS_PATH     Envoy와 공유하는 라우트 파일 경로
  TRAFFIC_ENVOY_ADMIN  Envoy 관리 주소(기본 http://127.0.0.1:9901)
  TRAFFIC_TARGET_HOST  대상이 Host 헤더로 서비스를 구분할 때 덮어쓸 호스트(선택)

#234: `traffic_prepare`는 대상 헬스 확인 결과를 `checks`로 돌려주고, `traffic_readiness`는 같은 실행의
데이터 이전(`data-<migration_id>`) 최근 값을 읽어 `traffic.ready`에 쓸 숫자를 돌려준다. 이벤트 발행은
Workflow가 `emit_migration_event`로 한다(중계 서버 워커에는 이벤트 저장소가 없다).
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from cmp_traffic import Handover, Sample, TrafficPolicy, check_handover, weights_for
from cmp_traffic.envoy import EnvoyObserver, EnvoyProxy
from temporalio import activity
from temporalio.exceptions import ApplicationError

HEALTH_TRIES = 3
HEALTH_TIMEOUT_S = 3.0


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


def health_status(host: str, port: int, path: str) -> int | None:
    """대상 헬스 경로의 HTTP 상태(연결 실패면 None). 중계 서버가 실제로 보낼 주소로 직접 묻는다."""
    url = f"http://{host}:{port}{path if path.startswith('/') else '/' + path}"
    request = urllib.request.Request(url, method="GET")
    if os.environ.get("TRAFFIC_TARGET_HOST"):
        request.add_header("Host", os.environ["TRAFFIC_TARGET_HOST"])
    try:
        with urllib.request.urlopen(request, timeout=HEALTH_TIMEOUT_S) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, ValueError):
        return None


async def check_health(host: str, port: int, path: str) -> int:
    """몇 번 물어 2xx·3xx면 그 상태를 돌려준다. 끝내 건강하지 않으면 전환을 시작하지 않는다."""
    status = None
    for attempt in range(HEALTH_TRIES):
        status = await asyncio.to_thread(health_status, host, port, path)
        if status is not None and 200 <= status < 400:
            return status
        if attempt + 1 < HEALTH_TRIES:
            await asyncio.sleep(1)
    raise ApplicationError(
        "target health check failed", {"status": status}, type="TargetUnhealthy", non_retryable=True
    )


@activity.defn
async def traffic_prepare(payload: dict) -> dict:
    """인계값을 확인하고 대상 헬스를 본 뒤 원본 기준선을 재고 원본 100% 설정을 깐다."""
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
    status = await check_health(wanted[0], wanted[1], handover.health_path)
    baseline = await asyncio.to_thread(observer.sample_origin, policy.observe_seconds)
    await asyncio.to_thread(proxy.apply, weights_for(0))
    checks = [
        {"id": "external_endpoint", "state": "done", "value": handover.external_endpoint},
        {"id": "health", "state": "done", "value": f"{handover.health_path} {status}"},
    ]
    return {"baseline": baseline.model_dump(), "checks": checks}


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


@activity.defn
async def traffic_readiness(payload: dict) -> dict:
    """같은 실행의 데이터 이전 최근 값 → `traffic.ready` 숫자. 데이터 이전이 없거나 읽지 못하면 `{}`.

    Workflow 큐(이벤트 저장소가 있는 워커)에서 돈다. 읽기만 하며 데이터 이전에 신호를 보내지 않는다.
    """
    migration_id = payload.get("migration_id")
    if not isinstance(migration_id, str) or not migration_id:
        return {}
    try:
        status = await activity.client().get_workflow_handle(f"data-{migration_id}").query("status")
    except Exception:  # noqa: BLE001 - no data migration in this run, or not reachable: no event
        return {}
    sample = status.get("last_sample") or {}
    readiness = status.get("last_readiness") or {}
    return {
        "lag_ms": sample.get("lag_ms"),
        "longest_tx_ms": readiness.get("longest_xact_ms"),
        "commits_per_sec": readiness.get("commits_per_sec"),
    }
