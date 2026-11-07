"""온프레 Agent가 플랫폼에 접속한 출발지 공인 IP를 기억하고, 데이터 터널의 허용 IP로 쓴다.

온프레 대상은 NAT 뒤에 있어서 원본 EKS의 공개 UDP 입구(NLB)가 받아야 하는 IP(`target_egress_cidrs`)를 사람이 적어 왔다.
그 IP는 와이파이(집·회장·핫스팟)마다 다르다. Agent는 이미 몇 초마다 `POST /agents/{id}/tunnel/status`를 HTTPS로
보내고, 이 요청은 공개 에지(Caddy)를 거쳐 오므로 Caddy가 붙인 `X-Forwarded-For`의 출발지가 곧 Agent의 NAT 공인 IP다
(UDP 터널도 같은 NAT로 나간다). 요청은 Agent 자격 증명으로 인증돼서 남이 IP를 바꿔 넣을 수 없다.

값은 메모리에만 둔다. API를 다시 띄우면 비워지지만 Agent가 곧 다시 보낸다. 오래된 값(TTL)은 쓰지 않는다.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections.abc import Callable

TTL_SECONDS = 600  # Agent가 이만큼 소식이 없으면 그 IP는 믿지 않는다(와이파이가 바뀌었을 수 있다)
MAX_CIDRS = 8  # WireGuardTunnel.target_egress_cidrs 상한


def client_ip(forwarded_for: str | None) -> str | None:
    """`X-Forwarded-For`에서 우리 에지가 붙인 마지막 값을 공인 IPv4로 돌려준다. 아니면 None.

    사설·루프백·링크로컬 주소는 에지를 거치지 않은 요청(도커 내부, 점검)이라 NAT 공인 IP가 아니므로 버린다.
    """
    if not forwarded_for:
        return None
    candidate = forwarded_for.split(",")[-1].strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if address.version != 4 or not address.is_global:
        return None
    return str(address)


class EgressStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, str]] = {}

    def put(self, agent_id: str, ip: str) -> None:
        with self._lock:
            self._items[agent_id] = (self._clock(), ip)

    def get(self, agent_id: str) -> str | None:
        """신선한 IP. 없거나 오래됐으면 None."""
        with self._lock:
            item = self._items.get(agent_id)
        if item is None or self._clock() - item[0] > TTL_SECONDS:
            return None
        return item[1]

    def age(self, agent_id: str) -> float | None:
        with self._lock:
            item = self._items.get(agent_id)
        return None if item is None else round(self._clock() - item[0], 1)


EGRESS = EgressStore()
