"""Tunnel configuration exchange API (ticket 16).

Agents use their existing Bearer credential (04A/15; revoked Agents are refused there). Relay
servers are registered by the operator with the enrollment admin key, get their own credential
once, and poll the Agent list with it (P1/T12).
"""

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    config_version: int


class TunnelStatusReport(Strict):
    tunnel_ip: str
    last_handshake_at: int | None = None
    app_health_ok: bool
    db_connect_ok: bool
    # Ticket 17 W8: why a check failed (code only, never free text); null when all checks pass.
    code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")] | None = None


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
    agent_id: str, report: TunnelStatusReport, session: SessionDep, credential: CredentialDep
):
    _agent(session, agent_id, credential)
    try:
        record_status(session, agent_id, **report.model_dump())
    except TunnelError as exc:
        raise _error(exc) from None
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


@router.get("/relays/{relay_id}/agents", response_model=RelayAgentList)
def get_relay_agents(relay_id: str, session: SessionDep, credential: CredentialDep):
    """P1: polled by the relay. Bearer = the relay credential returned at registration (T3)."""
    try:
        authenticate_relay(session, relay_id, credential)
    except TunnelError:
        raise HTTPException(status_code=403, detail="Relay unauthorized") from None
    return relay_agent_list(session)
