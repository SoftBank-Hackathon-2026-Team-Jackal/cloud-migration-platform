"""데모 장애 스위치 조작: 중계 서버에서 대상 앱의 `/__cmp/fault`를 켜고 끈다(이전 시연용).

대상 앱 주소는 이 중계 서버 Envoy가 지금 대상으로 보내는 곳(`cluster_target`)에서 읽는다. 공개 경로는
Envoy가 `/__cmp/`를 막으므로 대상 앱에 직접 보낸다. 토큰은 대상 앱의 `FAULT_TOKEN`과 같은 값이고 로그에 쓰지 않는다.

환경변수:
  FAULT_TOKEN       대상 앱의 FAULT_TOKEN과 같은 값(헤더 X-Fault-Token)
  ENVOY_ADMIN_URL   기본 http://127.0.0.1:9901
  FAULT_TARGET_URL  대상 앱 주소를 직접 줄 때(예: http://127.0.0.1:18003). 없으면 Envoy에서 읽는다

실행: python3 -m cmp_traffic.fault on [--error-rate 0.5] [--delay-ms 800] [--duration 300]
      python3 -m cmp_traffic.fault off
      python3 -m cmp_traffic.fault status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .envoy import cluster_target_address, http_get_json

PATH = "/__cmp/fault"
Send = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, Any]]]


def http_send(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def target_url(env: dict[str, str], fetch: Callable[[str], dict] = http_get_json) -> str:
    """대상 앱 주소. `FAULT_TARGET_URL`이 있으면 그 값, 없으면 Envoy `cluster_target`에서 읽는다."""
    given = env.get("FAULT_TARGET_URL", "").rstrip("/")
    if given:
        return given
    admin = env.get("ENVOY_ADMIN_URL", "http://127.0.0.1:9901").rstrip("/")
    address = cluster_target_address(fetch(f"{admin}/config_dump"))
    if address is None:
        raise RuntimeError("Envoy에서 대상 주소(cluster_target)를 읽지 못했습니다.")
    return f"http://{address[0]}:{address[1]}"


def command(action: str, error_rate: float = 0.5, delay_ms: int = 800, duration_s: int = 300):
    """장애 스위치에 보낼 (메서드, 본문). on은 켜기, off는 끄기, 그 밖은 상태 읽기."""
    if action == "on":
        body = {"error_rate": error_rate, "delay_ms": delay_ms, "duration_s": duration_s}
        return "POST", json.dumps(body).encode("utf-8")
    if action == "off":
        return "DELETE", None
    return "GET", None


def call(
    base: str, token: str, action: str, send: Send = http_send, **settings: Any
) -> tuple[int, dict[str, Any]]:
    """대상 앱 `/__cmp/fault`에 한 번 보낸다. 토큰은 헤더로만 보내고 돌려주지 않는다."""
    method, body = command(action, **settings)
    headers = {"X-Fault-Token": token, "Content-Type": "application/json"}
    return send(method, base + PATH, headers, body)


def request_for(args: argparse.Namespace) -> tuple[str, bytes | None]:
    if args.command == "on":
        return command("on", args.error_rate, args.delay_ms, args.duration)
    return command(args.command)


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python3 -m cmp_traffic.fault")
    sub = parser.add_subparsers(dest="command", required=True)
    on = sub.add_parser("on", help="장애를 켠다(정한 시간이 지나면 대상 앱이 스스로 끈다)")
    on.add_argument("--error-rate", type=float, default=0.5)
    on.add_argument("--delay-ms", type=int, default=800)
    on.add_argument("--duration", type=int, default=300)
    sub.add_parser("off", help="장애를 바로 끈다")
    sub.add_parser("status", help="지금 상태")
    return parser.parse_args(argv)


def main(
    argv: list[str],
    env: dict[str, str] | None = None,
    send: Send = http_send,
    fetch: Callable[[str], dict] = http_get_json,
) -> int:
    env = dict(os.environ) if env is None else env
    args = parse(argv)
    token = env.get("FAULT_TOKEN", "")
    if not token:
        print("FAULT_TOKEN이 필요합니다.", file=sys.stderr)
        return 2
    try:
        base = target_url(env, fetch)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"대상 주소를 알 수 없습니다: {exc}", file=sys.stderr)
        return 1
    method, body = request_for(args)
    headers = {"X-Fault-Token": token, "Content-Type": "application/json"}
    try:
        status, data = send(method, base + PATH, headers, body)
    except OSError as exc:
        print(f"대상 앱에 닿지 못했습니다: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"target": base, "status": status, **data}, ensure_ascii=False))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
