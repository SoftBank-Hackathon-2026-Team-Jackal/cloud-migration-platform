"""Tunnel configuration exchange API (ticket 16).

Agents use their existing Bearer credential (04A/15; revoked Agents are refused there). Relay
servers are registered by the operator with the enrollment admin key, get their own credential
once, and poll the Agent list with it (P1/T12).
"""

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cmp_api import agent_egress, pulse
from cmp_api.agent_registry import AgentRegistrationError, authenticate_agent
from cmp_api.routes.agents import CredentialDep, SessionDep, require_admin
from cmp_api.tunnels import (
    TunnelError,
    agent_config,
    authenticate_relay,
    config_version,
    record_status,
    register_key,
    relay_agent_list,
    relay_peers,
    upsert_relay,
)

router = APIRouter(tags=["tunnels"])
STATUS = {"TUNNEL_KEY_NOT_REGISTERED": 404, "TUNNEL_IP_MISMATCH": 409, "TUNNEL_CIDR_EXHAUSTED": 409}
Name = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,118}[a-z0-9])?$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _wireguard_public_key(value: str) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("WireGuard public key must be base64") from None
    if len(raw) != 32:
        raise ValueError("WireGuard public key must be 32 bytes")
    return value


class TunnelKeyRequest(Strict):
    public_key: str  # never a private key (T9)

    _key = field_validator("public_key")(lambda cls, v: _wireguard_public_key(v))


class RelayPeer(BaseModel):
    relay_id: str
    endpoint: str
    public_key: str


class TunnelKeyResponse(BaseModel):
    tunnel_ip: str
    relays: list[RelayPeer]  # P1: the Agent can start the tunnel without a second call
    config_version: int


class TunnelConfigResponse(BaseModel):
    tunnel_ip: str
    relays: list[RelayPeer]
    allowed_ips: list[str]
    mtu: int
    persistent_keepalive: int
    exposed_ports: list[int]
    # Ticket 73: the exposed port the Agent forwards to the K3s API (`kubernetes.default.svc:443`);
    # null while the operator has not enabled it.
    kube_api_port: int | None = None
    config_version: int


class TunnelStatusReport(Strict):
    tunnel_ip: str
    last_handshake_at: int | None = None
    app_health_ok: bool
    db_connect_ok: bool
    # Ticket 17 W8: why a check failed (code only, never free text); null when all checks pass.
    code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")] | None = None
    # Ticket 73: TCP connect to the K3s API port through the tunnel IP; null when not exposed.
    # Diagnostics only: it never changes `code` or the app/DB handoff checks.
    kube_api_ok: bool | None = None
    # Node CPU/memory the Agent adds for the server-pulse widget (target side). Optional: an Agent
    # without them, or a node it cannot read, simply leaves the target pulse empty.
    cpu_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    mem_used_mb: Annotated[int, Field(ge=0, le=10_000_000)] | None = None
    mem_total_mb: Annotated[int, Field(ge=1, le=10_000_000)] | None = None


class RelayRegistration(Strict):
    endpoint: Annotated[str, Field(pattern=r"^[A-Za-z0-9.-]{1,253}:[0-9]{1,5}$")]
    public_key: str

    _key = field_validator("public_key")(lambda cls, v: _wireguard_public_key(v))


class RelayAgent(BaseModel):
    agent_id: str
    public_key: str
    tunnel_ip: str
    allowed_ips: list[str]  # WireGuard peer AllowedIPs: the Agent's /32
    allowed_ports: list[int]  # empty when revoked
    # Ticket 74: which of `allowed_ports` is the K3s API (relay TCP forward); null when off/revoked.
    kube_api_port: int | None = None
    revoked: bool  # true: remove this peer


class RelayAgentList(BaseModel):
    list_version: str  # changes whenever any entry changes
    agents: list[RelayAgent]


class RelayRegistered(BaseModel):
    relay_id: str
    credential: str | None  # shown only when the relay is first registered
    config_version: int


def _agent(session, agent_id: str, credential: str) -> None:
    try:
        authenticate_agent(session, agent_id=agent_id, credential=credential)
    except AgentRegistrationError:
        raise HTTPException(status_code=403, detail="Agent unauthorized") from None


def _error(exc: TunnelError) -> HTTPException:
    return HTTPException(status_code=STATUS.get(exc.code, 409), detail=exc.code)


@router.put("/agents/{agent_id}/tunnel/key", response_model=TunnelKeyResponse)
def put_tunnel_key(
    agent_id: str, request: TunnelKeyRequest, session: SessionDep, credential: CredentialDep
):
    _agent(session, agent_id, credential)
    try:
        tunnel = register_key(session, agent_id, request.public_key)
    except TunnelError as exc:
        raise _error(exc) from None
    return {
        "tunnel_ip": tunnel.tunnel_ip,
        "relays": relay_peers(session),
        "config_version": config_version(session),
    }


@router.get("/agents/{agent_id}/tunnel/config", response_model=TunnelConfigResponse)
def get_tunnel_config(agent_id: str, session: SessionDep, credential: CredentialDep):
    _agent(session, agent_id, credential)
    try:
        return agent_config(session, agent_id)
    except TunnelError as exc:
        raise _error(exc) from None


@router.post("/agents/{agent_id}/tunnel/status", status_code=202)
def post_tunnel_status(
    agent_id: str,
    report: TunnelStatusReport,
    session: SessionDep,
    credential: CredentialDep,
    request: Request,
):
    _agent(session, agent_id, credential)
    # 인증된 Agent의 요청이 에지(Caddy)를 거쳐 온 출발지 = 그 온프레의 NAT 공인 IP. 터널 허용 IP 자동 채움에 쓴다.
    egress = agent_egress.client_ip(request.headers.get("x-forwarded-for"))
    if egress is not None:
        agent_egress.EGRESS.put(agent_id, egress)
    fields = report.model_dump()
    pulse_fields = {k: fields.pop(k) for k in ("cpu_percent", "mem_used_mb", "mem_total_mb")}
    try:
        record_status(session, agent_id, **fields)
    except TunnelError as exc:
        raise _error(exc) from None
    if all(value is not None for value in pulse_fields.values()):
        # Authenticated Agent report -> the target side of the server pulse (no shared token).
        pulse.REPORTS.put(
            "target",
            agent_id,
            pulse_fields["cpu_percent"],
            pulse_fields["mem_used_mb"],
            pulse_fields["mem_total_mb"],
        )
    return {"accepted": True}


@router.put(
    "/tunnel-admin/relays/{relay_id}",
    response_model=RelayRegistered,
    dependencies=[Depends(require_admin)],
)
def put_relay(relay_id: Name, request: RelayRegistration, session: SessionDep, response: Response):
    relay, credential = upsert_relay(session, relay_id, request.endpoint, request.public_key)
    response.status_code = 201 if credential else 200
    return {
        "relay_id": relay.id,
        "credential": credential,
        "config_version": config_version(session),
    }


@router.get(
    "/tunnel-admin/agents/{agent_id}/egress",
    dependencies=[Depends(require_admin)],
    summary="Public IP the Agent last reached the platform from (data tunnel allow-list)",
)
def get_agent_egress(agent_id: str) -> dict:
    """운영자 확인용: 이 Agent의 마지막 접속 출발지 IP와 몇 초 전 값인지. 값이 없으면 `egress_ip`는 null이다.
    `auto_target_egress`가 켜진 이전은 시작할 때 이 값을 터널 허용 목록에 더한다. 오래된(10분 넘은) 값은 쓰지 않는다."""
    return {
        "agent_id": agent_id,
        "egress_ip": agent_egress.EGRESS.get(agent_id),
        "age_seconds": agent_egress.EGRESS.age(agent_id),
    }


@router.get("/relays/{relay_id}/agents", response_model=RelayAgentList)
def get_relay_agents(relay_id: str, session: SessionDep, credential: CredentialDep):
    """P1: polled by the relay. Bearer = the relay credential returned at registration (T3)."""
    try:
        authenticate_relay(session, relay_id, credential)
    except TunnelError:
        raise HTTPException(status_code=403, detail="Relay unauthorized") from None
    return relay_agent_list(session)
