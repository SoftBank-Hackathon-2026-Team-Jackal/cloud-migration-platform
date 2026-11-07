"""시연용 장애 스위치 제어(`/system/fault`): Tools 화면이 정한 "원하는 장애 상태"와 대상 앱의 실제 상태.

플랫폼은 대상 앱에 직접 닿지 않는다(중계 서버 Envoy가 공개 경로의 `/__cmp/`를 막는다). 그래서 원하는 상태만
여기에 두고, 중계 서버 리포터(`cmp_traffic.health`)가 5초마다 보고할 때 응답으로 받아 가서 대상 앱에 적용한다.
리포터는 대상 앱에서 읽은 실제 상태를 다음 보고에 실어 보낸다. 화면은 "요청함"과 "실제로 켜짐"을 구분해 보인다.

명령마다 새 id를 붙여 리포터가 같은 명령을 한 번만 적용하게 한다(플랫폼이 다시 떠도 id가 겹치지 않는다).
켜기 명령은 정한 시간이 지나면 내주지 않는다: 늦게 붙은 리포터가 지난 명령을 적용하지 않게 한다.
값은 메모리에만 둔다(재시작하면 비워지고, 대상 앱의 장애도 정한 시간이 지나면 저절로 꺼진다).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable

from cmp_contracts.events import utc_now

OBSERVED_STALE_SECONDS = 20


class FaultControl:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._desired: dict | None = None
        self._desired_at = 0.0
        self._observed: dict | None = None
        self._observed_at = 0.0

    def turn_on(self, error_rate: float, delay_ms: int, duration_s: int) -> dict:
        command = {
            "id": uuid.uuid4().hex,
            "action": "on",
            "error_rate": error_rate,
            "delay_ms": delay_ms,
            "duration_s": duration_s,
            "requested_at": utc_now(),
        }
        with self._lock:
            self._desired, self._desired_at = command, self._clock()
        return command

    def turn_off(self) -> dict:
        command = {"id": uuid.uuid4().hex, "action": "off", "requested_at": utc_now()}
        with self._lock:
            self._desired, self._desired_at = command, self._clock()
        return command

    def desired(self) -> dict | None:
        """리포터에 내줄 명령. 기간이 지난 켜기 명령은 내주지 않는다."""
        with self._lock:
            command, at = self._desired, self._desired_at
        if command and command["action"] == "on" and self._clock() - at > command["duration_s"]:
            return None
        return command

    def observe(self, state: dict) -> None:
        with self._lock:
            self._observed, self._observed_at = state, self._clock()

    def clear(self) -> None:
        with self._lock:
            self._desired = self._observed = None

    def view(self) -> dict:
        now = self._clock()
        command = self.desired()
        with self._lock:
            observed, observed_at = self._observed, self._observed_at
        fresh = observed is not None and now - observed_at <= OBSERVED_STALE_SECONDS
        return {
            "at": utc_now(),
            "desired": command,
            "observed": observed if fresh else None,
            "observed_age_seconds": round(now - observed_at, 1) if observed is not None else None,
            # 리포터가 마지막 명령을 대상 앱에 적용했는가(명령이 없으면 None)
            "applied": (observed.get("applied_id") == command["id"]) if fresh and command else None,
        }


CONTROL = FaultControl()
