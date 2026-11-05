"""Provisioning 결과(ProvisioningResult JSON)를 트래픽 인계값(Handover)으로 바꾼다(#128).

두 모델은 서로 다른 팀이 만들었다. 이 함수가 유일한 연결점이라 이름·규칙이 바뀌면 여기만 고친다.
안전 규칙: READY가 아니거나 앱이 ready가 아니거나 TLS가 켜져 있으면(P0는 HTTP만) ready=False로 만들어
`check_handover`가 전환을 시작하지 못하게 한다.
"""

from __future__ import annotations

from collections.abc import Mapping

from .contracts import Handover

_STATUS = {
    "READY": "READY",
    "FAILED": "FAILED",
}  # INFRASTRUCTURE_READY·UNKNOWN은 확인 전이라 UNKNOWN


def handover_from_result(result: Mapping) -> Handover:
    status = _STATUS.get(str(result.get("status")), "UNKNOWN")
    app = result.get("application") or {}
    host, port = app.get("external_host"), app.get("external_port") or app.get("port")
    # 호스트·포트를 따로 읽는 것이 정본이다. 합쳐진 external_endpoint는 호환용이라 둘이 없을 때만 쓴다.
    endpoint = f"http://{host}:{port}" if host and port else app.get("external_endpoint")
    http_only = app.get("scheme", "http") == "http" and not app.get("tls")
    return Handover(
        status=status,
        ready=status == "READY" and bool(app.get("ready")) and http_only,
        external_endpoint=endpoint,
        health_path=app.get("health_path") or "/health",
        port=int(port or 80),
    )
