"""그림자 리허설 Activity(Provisioning 83, #362). 중계 서버 워커(relay 큐)에서 돈다.

중계 Envoy가 허용 경로의 GET만 같은 호스트의 그림자 수집기(`cmp_traffic.shadow`)로 복사하게 켜고 끈다.
결과는 경고·표시용이다. 자동 차단·자동 수정·DB 설정 변경·쓰기 복사·Pod 재시작은 하지 않는다.

계약(84 Workflow·85 웹과 공유):
  shadow_start({migration_id, run_id, allow_paths, percent, rate_per_sec}) -> {"ok", "code"}
  shadow_summary({run_id}) -> 수집기 요약(실패하면 ApplicationError type=SHADOW_UNAVAILABLE, 재시도 안 함)
  shadow_stop({run_id}) -> {"ok", "code"}. 멱등. 수집기가 죽어 있어도 Envoy 복사 끄기는 반드시 시도한다.

환경변수: TRAFFIC_SHADOW_API(기본 http://127.0.0.1:18091) + 트래픽 Activity와 같은 TRAFFIC_* 설정.
가중치 Activity(`traffic_set_weight` 등)는 복사를 모르므로, 켜진 채 가중치를 바꾸면 복사가 꺼진다(안전한 쪽).
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cmp_traffic.envoy import ShadowMirror
from temporalio import activity
from temporalio.exceptions import ApplicationError

from cmp_worker.activities import traffic

UNAVAILABLE = "SHADOW_UNAVAILABLE"
API_TIMEOUT_S = 3.0


def _api() -> str:
    return (os.environ.get("TRAFFIC_SHADOW_API") or "http://127.0.0.1:18091").rstrip("/")


def _call(method: str, path: str, doc: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """수집기 제어 API 호출. (HTTP 상태, JSON). 연결하지 못하면 OSError."""
    data = json.dumps(doc).encode() if doc is not None else None
    request = urllib.request.Request(_api() + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        return exc.code, body


def _run_path(run_id: str) -> str:
    return "/runs/" + urllib.parse.quote(str(run_id), safe="")


def _unavailable() -> dict[str, Any]:
    return {"ok": False, "code": UNAVAILABLE}


async def _end_run(run_id: str) -> None:
    try:
        await asyncio.to_thread(_call, "DELETE", _run_path(run_id))
    except (OSError, ValueError):
        activity.logger.warning("shadow collector did not answer the stop for run %s", run_id)


@activity.defn
async def shadow_start(payload: dict) -> dict:
    """수집기에 실행을 먼저 등록하고(복사본이 갈 곳) Envoy 복사를 켠다. 어느 쪽이든 실패하면 켜지 않는다."""
    run_id = str(payload.get("run_id") or "")
    try:
        mirror = ShadowMirror(
            allow_paths=tuple(payload.get("allow_paths") or ()),
            percent=int(payload.get("percent") or 100),
        )
        proxy, _ = traffic.build_ports()
    except (ValueError, TypeError, ApplicationError) as exc:
        activity.logger.warning("shadow start refused: %s", exc)
        return _unavailable()
    spec = {
        "run_id": run_id,
        "migration_id": payload.get("migration_id"),
        "allow_paths": list(mirror.allow_paths),
        "percent": mirror.percent,
        "rate_per_sec": int(payload.get("rate_per_sec") or 20),
    }
    try:
        status, _ = await asyncio.to_thread(_call, "POST", "/runs", spec)
    except (OSError, ValueError) as exc:
        activity.logger.warning("shadow collector unreachable: %s", exc)
        return _unavailable()
    if status != 200:
        activity.logger.warning("shadow collector refused the run (%s)", status)
        return _unavailable()
    try:
        await asyncio.to_thread(proxy.set_shadow, mirror)
    except (RuntimeError, OSError) as exc:
        activity.logger.warning("envoy did not take the shadow copy: %s", exc)
        await _end_run(run_id)
        return _unavailable()
    return {"ok": True, "code": None}


@activity.defn
async def shadow_summary(payload: dict) -> dict:
    """수집기 요약을 그대로 돌려준다. 수집기에 닿지 못하거나 실행을 모르면 SHADOW_UNAVAILABLE."""
    run_id = str(payload.get("run_id") or "")
    try:
        status, body = await asyncio.to_thread(_call, "GET", _run_path(run_id) + "/summary")
    except (OSError, ValueError) as exc:
        raise ApplicationError(
            f"shadow collector unreachable: {exc}", type=UNAVAILABLE, non_retryable=True
        ) from exc
    if status != 200:
        raise ApplicationError(
            f"shadow summary not available ({status})", type=UNAVAILABLE, non_retryable=True
        )
    return body


@activity.defn
async def shadow_stop(payload: dict) -> dict:
    """Envoy 복사를 먼저 끄고 수집기 실행을 멈춘다. 멱등. 수집기 응답과 무관하게 Envoy 끄기를 시도한다.

    `ok`는 Envoy 복사가 꺼졌는지다(사용자 경로의 상태). 수집기가 죽어 있으면 복사본이 갈 곳이 없을 뿐이다.
    """
    run_id = str(payload.get("run_id") or "")
    envoy_off = False
    try:
        proxy, _ = traffic.build_ports()
        await asyncio.to_thread(proxy.set_shadow, None)
        envoy_off = True
    except (RuntimeError, OSError, ApplicationError) as exc:
        activity.logger.error("could not turn the envoy shadow copy off: %s", exc)
    await _end_run(run_id)
    return {"ok": True, "code": None} if envoy_off else _unavailable()
