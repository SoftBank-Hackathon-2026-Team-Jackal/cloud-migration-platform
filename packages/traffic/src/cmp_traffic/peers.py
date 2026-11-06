"""중계 서버 WireGuard peer 동기화(#63).

플랫폼 조회 API(`GET /relays/{relay_id}/agents`, docs/contracts/openapi.yaml의 RelayAgentList)를
주기적으로 읽어 `wg` peer를 맞춘다. 판단은 순수 함수이고
외부 호출(조회 API, wg 실행)은 포트(source, wg)로 분리했다. 비밀값(개인키)은 다루지 않는다.

안전 규칙: 잘못된 목록(범위 밖·예약 주소·중복·형식 오류)은 통째로 거부하고 현재 상태를 유지한다.
빈 목록은 실수로 모든 peer를 지우지 않도록 명시적으로 허용할 때만 처리한다.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

KEEPALIVE = 25  # NAT 뒤 Agent가 터널을 유지하도록 고정(실측·합의 값)


class AgentPeer(BaseModel):
    """플랫폼 RelayAgent. 폐기된 Agent도 목록에 남아(`revoked: true`) 중계 서버가 peer를 지운다."""

    agent_id: str = Field(min_length=1)
    public_key: str
    tunnel_ip: str
    allowed_ips: list[str]
    allowed_ports: list[int] = Field(default_factory=list)
    # 티켓 74: 그중 K3s API 포트(중계 서버 TCP 전달 대상). 예전 플랫폼·꺼짐·폐기면 None.
    kube_api_port: int | None = None
    revoked: bool

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

    @model_validator(mode="after")
    def _only_its_own_tunnel_address(self) -> AgentPeer:
        # Agent가 받을 수 있는 주소는 자기 터널 IP(/32) 하나뿐이다. 넓은 대역은 목록 전체를 거부한다.
        if self.allowed_ips != [f"{self.tunnel_ip}/32"]:
            raise ValueError("allowed_ips는 터널 IP의 /32 하나여야 합니다.")
        return self


class AgentList(BaseModel):
    list_version: str
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
        if not agent.revoked:
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
        forwards: Callable[[AgentList], object] | None = None,
    ) -> None:
        self.source, self.wg = source, wg
        self.tunnel_cidr, self.interface, self.allow_empty = tunnel_cidr, interface, allow_empty
        self.forwards = forwards  # 티켓 74: peer 적용 뒤 K3s API 전달 파일을 맞춘다(없으면 끔)
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
        if self.forwards is not None:
            self.forwards(listing)  # 목록 검증(plan_peer_changes)을 통과한 뒤에만
        self.version = listing.list_version  # 모두 적용된 뒤에만 버전을 올린다
        return plan


RELAY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def relay_agents_url(base_url: str, relay_id: str) -> str:
    """플랫폼 기본 주소와 중계 서버 ID로 조회 주소를 만든다. ID는 경로를 바꾸지 못하게 검사한다."""
    if not RELAY_ID.fullmatch(relay_id):
        raise ValueError("relay_id 형식이 아닙니다.")
    return f"{base_url.rstrip('/')}/relays/{relay_id}/agents"


class HttpAgentSource:
    """플랫폼 조회 API에서 Agent 목록을 읽는다.

    서버는 조건부 요청(304)을 주지 않으므로 매번 받아 `list_version`이 같으면 None(변경 없음)으로 본다.
    token(중계 서버 자격증명)은 설정으로만 받고 로그나 예외 메시지에 쓰지 않는다.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        timeout: float = 5.0,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self.url, self.token, self.timeout, self.opener = url, token, timeout, opener

    def fetch(self, known_version: str | None) -> AgentList | None:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, headers=headers)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                listing = AgentList.model_validate(json.loads(response.read()))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"agent list request failed: HTTP {exc.code}") from exc
        return None if listing.list_version == known_version else listing


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


def run_sync_loop(
    syncer: PeerSyncer,
    interval: float,
    *,
    sleep: Callable[[float], object] = time.sleep,
    log: Callable[[str], object] = print,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """주기적으로 동기화한다. 한 번 실패해도(조회 오류·잘못된 목록) 현재 peer를 그대로 두고 계속한다."""
    while not should_stop():
        try:
            plan = syncer.sync_once()
            if plan and (plan.add or plan.remove):
                log(f"peer sync: +{len(plan.add)} -{len(plan.remove)} (version {syncer.version})")
        except Exception as exc:  # noqa: BLE001 - 상시 루프는 어떤 실패에도 죽지 않고 현재 peer를 유지한다
            log(f"peer sync failed, keeping current peers: {type(exc).__name__}: {exc}")
        sleep(interval)


def config_from_env(env: Mapping[str, str]) -> dict:
    missing = [k for k in ("PLATFORM_URL", "RELAY_ID", "RELAY_CREDENTIAL") if not env.get(k)]
    if missing:
        raise ValueError("필요한 환경변수가 없습니다: " + ", ".join(missing))
    return {
        "url": relay_agents_url(env["PLATFORM_URL"], env["RELAY_ID"]),
        "token": env["RELAY_CREDENTIAL"],
        "tunnel_cidr": env.get("TUNNEL_CIDR", "10.200.0.0/24"),
        "interface": env.get("WG_INTERFACE", "wg0"),
        "interval": float(env.get("SYNC_INTERVAL", "30")),
        # 티켓 74: 리슨 주소(중계 서버 사설 IP)를 줄 때만 K3s API 전달 파일을 쓴다.
        "kube_forward": (
            {
                "bind_ip": env["KUBE_FORWARD_BIND_IP"],
                "directory": env.get("KUBE_FORWARD_DIR", "/etc/envoy-kube"),
                "base_port": int(env.get("KUBE_FORWARD_BASE_PORT", "16400")),
            }
            if env.get("KUBE_FORWARD_BIND_IP")
            else None
        ),
    }


def main() -> int:
    try:
        config = config_from_env(os.environ)
    except ValueError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    forwards = None
    if config["kube_forward"]:
        from .kube_forward import KubeForwardFiles  # peers만 쓰는 서버는 yaml 없이도 돈다

        try:
            forwards = KubeForwardFiles(
                tunnel_cidr=config["tunnel_cidr"], **config["kube_forward"]
            ).apply
        except ValueError as exc:
            print(f"설정 오류: {exc}", file=sys.stderr)
            return 2
    syncer = PeerSyncer(
        HttpAgentSource(config["url"], token=config["token"]),
        WgCli(config["interface"]),
        config["tunnel_cidr"],
        config["interface"],
        forwards=forwards,
    )
    run_sync_loop(syncer, config["interval"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
