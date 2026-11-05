"""중계 서버 WireGuard peer 동기화(#63).

플랫폼 조회 API(Agent 목록)를 주기적으로 읽어 `wg` peer를 맞춘다. 판단은 순수 함수이고
외부 호출(조회 API, wg 실행)은 포트(source, wg)로 분리했다. 비밀값(개인키)은 다루지 않는다.

안전 규칙: 잘못된 목록(범위 밖·예약 주소·중복·형식 오류)은 통째로 거부하고 현재 상태를 유지한다.
빈 목록은 실수로 모든 peer를 지우지 않도록 명시적으로 허용할 때만 처리한다.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator

KEEPALIVE = 25  # NAT 뒤 Agent가 터널을 유지하도록 고정(실측·합의 값)


class AgentPeer(BaseModel):
    agent_id: str = Field(min_length=1)
    public_key: str
    tunnel_ip: str
    allowed_ports: list[int] = Field(default_factory=list)
    status: Literal["ACTIVE", "REVOKED"] = "ACTIVE"

    @field_validator("public_key")
    @classmethod
    def _wireguard_key(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("WireGuard 공개키 형식이 아닙니다.") from exc
        if len(raw) != 32 or len(value) != 44:
            raise ValueError("WireGuard 공개키 형식이 아닙니다.")
        return value

    @field_validator("allowed_ports")
    @classmethod
    def _ports(cls, value: list[int]) -> list[int]:
        if any(not 1 <= port <= 65535 for port in value):
            raise ValueError("허용 포트는 1~65535여야 합니다.")
        return value


class AgentList(BaseModel):
    version: str
    agents: list[AgentPeer]


class PeerPlan(BaseModel):
    add: list[AgentPeer] = Field(default_factory=list)  # 새로 넣거나 주소가 바뀐 peer
    remove: list[str] = Field(default_factory=list)  # 지울 공개키


def _check_tunnel_ip(value: str, network: ipaddress.IPv4Network) -> ipaddress.IPv4Address:
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValueError(f"터널 IP 형식이 아닙니다: {value!r}") from exc
    # 네트워크·브로드캐스트 주소와 중계 서버 자리(첫 주소)는 Agent에게 줄 수 없다.
    if ip not in network or ip in (network.network_address, network.broadcast_address):
        raise ValueError(f"터널 대역 밖이거나 예약된 주소입니다: {value}")
    if ip == network.network_address + 1:
        raise ValueError(f"중계 서버 주소는 Agent에게 줄 수 없습니다: {value}")
    return ip


def plan_peer_changes(
    current: dict[str, str],
    desired: AgentList,
    tunnel_cidr: str,
    allow_empty: bool = False,
) -> PeerPlan:
    """current: {공개키: 'ip/32'}. 목록이 잘못되면 ValueError(아무것도 적용하지 않는다)."""
    network = ipaddress.IPv4Network(tunnel_cidr)
    if not desired.agents and current and not allow_empty:
        raise ValueError("Agent 목록이 비어 있습니다. 모든 peer를 지울 수 없어 거부합니다.")

    keys: set[str] = set()
    ips: set[str] = set()
    wanted: dict[str, AgentPeer] = {}
    for agent in desired.agents:
        ip = _check_tunnel_ip(agent.tunnel_ip, network)
        if agent.public_key in keys or str(ip) in ips:
            raise ValueError(f"공개키 또는 터널 IP가 중복됩니다: {agent.agent_id}")
        keys.add(agent.public_key)
        ips.add(str(ip))
        if agent.status == "ACTIVE":
            wanted[agent.public_key] = agent

    add = [
        agent
        for public_key, agent in wanted.items()
        if current.get(public_key) != f"{agent.tunnel_ip}/32"
    ]
    remove = [public_key for public_key in current if public_key not in wanted]
    return PeerPlan(add=add, remove=remove)


def wg_commands(plan: PeerPlan, interface: str = "wg0") -> list[list[str]]:
    """셸을 거치지 않는 인자 목록. 삭제를 먼저 해서 주소 재사용 충돌을 피한다."""
    commands = [["wg", "set", interface, "peer", key, "remove"] for key in plan.remove]
    commands += [
        [
            "wg", "set", interface, "peer", agent.public_key,
            "allowed-ips", f"{agent.tunnel_ip}/32",
            "persistent-keepalive", str(KEEPALIVE),
        ]
        for agent in plan.add
    ]  # fmt: skip
    return commands


class AgentSource(Protocol):
    def fetch(self, known_version: str | None) -> AgentList | None:
        """알고 있는 버전이 최신이면 None(변경 없음)."""


class WgPort(Protocol):
    def current_peers(self) -> dict[str, str]: ...

    def run(self, argv: list[str]) -> None: ...


class PeerSyncer:
    def __init__(
        self,
        source: AgentSource,
        wg: WgPort,
        tunnel_cidr: str,
        interface: str = "wg0",
        allow_empty: bool = False,
    ) -> None:
        self.source, self.wg = source, wg
        self.tunnel_cidr, self.interface, self.allow_empty = tunnel_cidr, interface, allow_empty
        self.version: str | None = None

    def sync_once(self) -> PeerPlan | None:
        listing = self.source.fetch(self.version)
        if listing is None:
            return None
        plan = plan_peer_changes(
            self.wg.current_peers(), listing, self.tunnel_cidr, self.allow_empty
        )
        for argv in wg_commands(plan, self.interface):
            self.wg.run(argv)
        self.version = listing.version  # 모두 적용된 뒤에만 버전을 올린다
        return plan


class HttpAgentSource:
    """플랫폼 조회 API에서 Agent 목록을 읽는다. 버전이 같으면 304로 받아 None을 돌려준다.

    token은 설정(환경변수 등)으로만 받고 로그나 예외 메시지에 쓰지 않는다.
    """

    def __init__(self, url: str, token: str | None = None, timeout: float = 5.0) -> None:
        self.url, self.token, self.timeout = url, token, timeout

    def fetch(self, known_version: str | None) -> AgentList | None:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if known_version:
            headers["If-None-Match"] = known_version
        request = urllib.request.Request(self.url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return AgentList.model_validate(json.loads(response.read()))
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None
            raise RuntimeError(f"agent list request failed: HTTP {exc.code}") from exc


class WgCli:
    """`wg` 명령으로 현재 peer를 읽고 바꾼다(셸을 거치지 않는다)."""

    def __init__(
        self,
        interface: str = "wg0",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.interface, self.runner = interface, runner

    def current_peers(self) -> dict[str, str]:
        result = self.runner(
            ["wg", "show", self.interface, "allowed-ips"],
            check=True,
            capture_output=True,
            text=True,
        )
        peers: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, _, ips = line.partition("\t")
            first = ips.split()[0] if ips.split() else ""
            if key and "/" in first:  # 주소가 없는 peer는 "(none)"으로 나온다
                peers[key] = first
        return peers

    def run(self, argv: list[str]) -> None:
        self.runner(argv, check=True)
