"""트래픽 전환 Activity(#63). 프록시(Envoy) 호출 같은 외부 I/O는 여기서만 한다.

중계 서버 위치는 환경변수로 받는다(비밀값 아님). 설정이 없으면 전환하는 척하지 않고 실패한다.
  TRAFFIC_RDS_PATH     Envoy와 공유하는 라우트 파일 경로
  TRAFFIC_ENVOY_ADMIN  Envoy 관리 주소(기본 http://127.0.0.1:9901)
  TRAFFIC_TARGET_HOST  대상이 Host 헤더로 서비스를 구분할 때 덮어쓸 호스트(선택)
  TRAFFIC_CDS_PATH     Envoy와 공유하는 대상 클러스터 파일 경로(선택). 있으면 이전이 넘긴 대상 주소로 바꾼다.
                       없으면(정적 설정) 대상이 인계값과 다를 때 전환을 시작하지 않는다.
  TRAFFIC_TARGET_ALLOWED_CIDRS  대상 주소를 바꿔도 되는 범위(쉼표 구분, 기본 10.200.0.0/24 = 터널 대역)
  TRAFFIC_STREAM_PREFIXES  제한시간·재시도 없이 보낼 실시간 연결 경로(쉼표 구분, 선택, 예: /api/stream).
                       비어 있으면 라우트는 예전과 같다(#294).

#idle-probe: `traffic_observe`는 같은 창의 원본 요청 수도 싣고, `traffic_probe`는 사용자 트래픽이 없을 때
대상으로 합성 요청을 보내 판정에 쓸 값을 만든다.

#234: `traffic_prepare`는 대상 헬스 확인 결과를 `checks`로 돌려주고, `traffic_readiness`는 같은 실행의
데이터 이전(`data-<migration_id>`) 최근 값을 읽어 `traffic.ready`에 쓸 숫자를 돌려준다. 이벤트 발행은
Workflow가 `emit_migration_event`로 한다(중계 서버 워커에는 이벤트 저장소가 없다).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from cmp_traffic import (
    Handover,
    Sample,
    TrafficPolicy,
    check_handover,
    summarize_probe,
    weights_for,
)
from cmp_traffic.envoy import EnvoyObserver, EnvoyProxy
from cmp_traffic.probe import MAX_PROBE_REQUESTS
from temporalio import activity
from temporalio.exceptions import ApplicationError

HEALTH_TRIES = 3
HEALTH_TIMEOUT_S = 3.0
DEFAULT_ALLOWED_CIDRS = "10.200.0.0/24"  # 플랫폼 터널 대역(cmp_api.tunnels.TUNNEL_CIDR)
RETARGET_CONFIRM_S = 15.0  # 새 대상이 Envoy에서 활성이 되기를 기다리는 시간(헬스 체크 한 번 포함)
RETARGET_POLL_S = 0.5
PROBE_WORKERS = 5  # 합성 요청을 동시에 보내는 수(대상에 부담을 주지 않는 작은 값)


def handover_target(handover: Handover) -> tuple[str, int] | None:
    """핸드오버의 외부 주소를 (호스트, 포트)로. 없거나 읽을 수 없으면 None."""
    parsed = urlparse(handover.external_endpoint or "")
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or handover.port


def target_allowed(host: str) -> bool:
    """대상 주소를 바꿔도 되는 곳인가. IP 주소가 허용 범위 안일 때만 True(이름은 범위를 알 수 없어 거절한다).

    인계값은 플랫폼이 만들지만, 중계 서버가 엉뚱한 곳으로 트래픽을 보내지 않도록 범위를 한 번 더 막는다.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    allowed = os.environ.get("TRAFFIC_TARGET_ALLOWED_CIDRS") or DEFAULT_ALLOWED_CIDRS
    for item in allowed.split(","):
        try:
            if address in ipaddress.ip_network(item.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def stream_prefixes_from_env() -> tuple[str, ...]:
    """`TRAFFIC_STREAM_PREFIXES`(쉼표 구분)를 경로 목록으로. 빈 항목은 건너뛴다."""
    raw = os.environ.get("TRAFFIC_STREAM_PREFIXES") or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def build_ports() -> tuple[EnvoyProxy, EnvoyObserver]:
    rds = os.environ.get("TRAFFIC_RDS_PATH")
    if not rds:
        raise ApplicationError(
            "traffic relay is not configured (TRAFFIC_RDS_PATH)",
            type="RelayNotConfigured",
            non_retryable=True,
        )
    admin = os.environ.get("TRAFFIC_ENVOY_ADMIN", "http://127.0.0.1:9901")
    cds = os.environ.get("TRAFFIC_CDS_PATH")
    try:
        proxy = EnvoyProxy(
            Path(rds),
            admin,
            target_host=os.environ.get("TRAFFIC_TARGET_HOST") or None,
            cds_path=Path(cds) if cds else None,
            stream_prefixes=stream_prefixes_from_env(),
        )
    except ValueError as exc:  # 잘못된 TRAFFIC_STREAM_PREFIXES는 다시 해도 같다
        raise ApplicationError(str(exc), type="RelayNotConfigured", non_retryable=True) from exc
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


async def retarget(
    proxy: EnvoyProxy, observer: EnvoyObserver, wanted: tuple[str, int], health_path: str
) -> None:
    """중계 서버의 대상 주소를 인계값의 목적지로 바꾸고, Envoy에서 활성이 될 때까지 확인한다.

    안전 조건(하나라도 아니면 `TargetMismatch`로 멈춘다, 재시도하지 않는다):
    - 동적 대상(CDS 경로)으로 깔려 있다. 정적 설정은 바꿀 수 없다.
    - 새 주소가 허용 범위 안의 IP다.
    - 지금 대상으로 가는 트래픽이 0%다(전환 중인 대상을 바꾸지 않는다).
    """

    def refuse(why: str) -> ApplicationError:
        return ApplicationError(
            f"relay target does not match the handover ({why})",
            type="TargetMismatch",
            non_retryable=True,
        )

    if proxy.cds_path is None:
        raise refuse("static target")
    if not target_allowed(wanted[0]):
        raise refuse("target address not allowed")
    if await asyncio.to_thread(proxy.target_weight) != 0:
        raise refuse("traffic is already on the target")
    try:
        await asyncio.to_thread(proxy.set_target, wanted[0], wanted[1], health_path)
    except RuntimeError as exc:
        raise ApplicationError(str(exc), type="ApplyNotConfirmed", non_retryable=True) from exc
    waited = 0.0
    while waited < RETARGET_CONFIRM_S:
        if await asyncio.to_thread(observer.target_address) == wanted:
            return
        await asyncio.sleep(RETARGET_POLL_S)
        waited += RETARGET_POLL_S
    raise refuse("new target did not become active")


@activity.defn
async def traffic_prepare(payload: dict) -> dict:
    """인계값을 확인하고 대상 헬스를 본 뒤 원본 기준선을 재고 원본 100% 설정을 깐다."""
    handover = Handover.model_validate(payload["handover"])
    check = check_handover(handover)
    if not check.ok:
        raise ApplicationError(check.reason, type="HandoverNotReady", non_retryable=True)
    policy = TrafficPolicy.model_validate(payload.get("policy") or {})
    proxy, observer = build_ports()
    # 전환 전에 이 중계 서버의 대상이 인계값의 목적지와 같은지 본다. 다르면 동적 대상(CDS)으로 깔려 있고
    # 허용 범위 안일 때만 인계값의 주소로 바꾸고, 그렇지 않으면 엉뚱한 곳으로 보내지 않고 멈춘다.
    wanted = handover_target(handover)
    if wanted is None:
        raise ApplicationError(
            "relay target does not match the handover", type="TargetMismatch", non_retryable=True
        )
    actual = await asyncio.to_thread(observer.target_address)
    if actual != wanted:
        await retarget(proxy, observer, wanted, handover.health_path)
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
    """대상 쪽 한 관찰 창의 요청·실패·p99를 돌려준다. 같은 창의 원본 요청 수도 `origin_requests`로 싣는다.

    원본 요청 수는 "사용자 트래픽이 없다"(원본·대상 모두 적음)와 "가중치가 안 먹었다"(원본엔 있는데 대상만 0)를
    가르는 데 쓴다. 한 쪽만 읽는 관측기(옛 구현)이면 싣지 않고, 그러면 Workflow는 사용자 없음으로 단정하지 않는다.
    """
    _, observer = build_ports()
    seconds = payload["seconds"]
    pair = getattr(observer, "sample_pair", None)
    if pair is None:
        sample: Sample = await asyncio.to_thread(observer.sample_target, seconds)
        return sample.model_dump()
    origin, target = await asyncio.to_thread(pair, seconds)
    return {**target.model_dump(), "origin_requests": origin.requests}


def probe_once(host: str, port: int, path: str, timeout_s: float) -> tuple[int | None, float]:
    """대상 헬스 경로에 합성 요청 한 번. (HTTP 상태 또는 None, 응답 ms). 중계 서버가 쓰는 길로 직접 보낸다."""
    url = f"http://{host}:{port}{path if path.startswith('/') else '/' + path}"
    request = urllib.request.Request(url, method="GET")
    if os.environ.get("TRAFFIC_TARGET_HOST"):
        request.add_header("Host", os.environ["TRAFFIC_TARGET_HOST"])
    started = time.monotonic()
    status: int | None
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            response.read(1024)
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (OSError, ValueError):
        status = None
    return status, (time.monotonic() - started) * 1000


@activity.defn
async def traffic_probe(payload: dict) -> dict:
    """사용자 트래픽이 없을 때 대상으로 합성 요청을 보내 요청·실패·p99를 돌려준다(Sample).

    보내는 곳은 인계값의 대상뿐이고, 중계 서버가 이미 그곳으로 보내고 있거나 허용 범위(터널 대역) 안의 IP일
    때만 보낸다(엉뚱한 곳을 두드리지 않는다). 읽기 전용 GET만, 동시에 몇 건만 보낸다.
    """
    handover = Handover.model_validate(payload["handover"])
    wanted = handover_target(handover)
    if wanted is None:
        raise ApplicationError(
            "probe target not found in the handover", type="TargetMismatch", non_retryable=True
        )
    _, observer = build_ports()
    if not target_allowed(wanted[0]) and await asyncio.to_thread(observer.target_address) != wanted:
        raise ApplicationError(
            "probe target is outside the allowed range", type="TargetMismatch", non_retryable=True
        )
    count = max(1, min(MAX_PROBE_REQUESTS, int(payload["count"])))
    timeout_s = float(payload.get("timeout_s") or 3.0)

    def run() -> list[tuple[int | None, float]]:
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            return list(
                pool.map(
                    lambda _: probe_once(wanted[0], wanted[1], handover.health_path, timeout_s),
                    range(count),
                )
            )

    return summarize_probe(await asyncio.to_thread(run)).model_dump()


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


@activity.defn
async def traffic_db_gate(payload: dict) -> dict:
    """같은 실행의 데이터 이전 DB 게이트(쓰기 주체, 역방향 복제). 데이터 이전이 없거나 읽지 못하면 `{}`.

    안정화 구간에서 되돌린 뒤 다시 전환할 때, DB가 대상으로 다시 전환됐는지 확인하려고 Workflow 큐에서 읽는다.
    """
    migration_id = payload.get("migration_id")
    if not isinstance(migration_id, str) or not migration_id:
        return {}
    try:
        return await activity.client().get_workflow_handle(f"data-{migration_id}").query("db_gate")
    except Exception:  # noqa: BLE001 - no data migration in this run, or not reachable
        return {}
