"""Tunnel configuration exchange between on-prem Agents and relay servers (ticket 16, T2~T9).

The platform only relays *public* WireGuard material: Agent public keys and tunnel IPs one way,
relay endpoints and public keys the other way. Private keys never reach any API. Results stay
immutable (13 R3); tunnel checks are merged into the handoff view at read time (T6).

Not here yet (waiting for the Traffic owner, ticket 16 "답변 대기"): the relay-side Agent list (P1)
and filling the ON_PREM `external_endpoint` (P2).
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from ipaddress import IPv4Network

from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from cmp_api.db import Base
from cmp_api.provisioning_results import ProvisioningResultRecord

TUNNEL_CIDR = "10.200.0.0/24"  # T4 default; P3 may change it (config only)
RELAY_HOST_OFFSET = 1  # .1 is reserved for the relay server side
MTU = 1420
PERSISTENT_KEEPALIVE = 25
DB_PORT = 5432


class TunnelError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AgentTunnel(Base):
    __tablename__ = "agent_tunnels"

    agent_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    tunnel_ip: Mapped[str] = mapped_column(String(45), nullable=False, unique=True)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class RelayServer(Base):
    __tablename__ = "relay_servers"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class TunnelSettings(Base):
    """Single row holding the monotonically increasing relay config version (T8)."""

    __tablename__ = "tunnel_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)


class TunnelStatus(Base):
    __tablename__ = "agent_tunnel_status"

    agent_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    tunnel_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    last_handshake_at: Mapped[int | None] = mapped_column(Integer)
    app_health_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    db_connect_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reported_at: Mapped[int] = mapped_column(Integer, nullable=False)


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def config_version(session: Session) -> int:
    row = session.get(TunnelSettings, 1)
    return row.config_version if row else 0


def _allocate(session: Session) -> str:
    network = IPv4Network(TUNNEL_CIDR)
    used = set(session.scalars(select(AgentTunnel.tunnel_ip)))
    for index, host in enumerate(network.hosts(), start=1):
        if index <= RELAY_HOST_OFFSET:
            continue
        if str(host) not in used:
            return str(host)
    raise TunnelError("TUNNEL_CIDR_EXHAUSTED")


def register_key(session: Session, agent_id: str, public_key: str) -> AgentTunnel:
    """Register or rotate an Agent public key; the tunnel IP is kept across rotations (T4)."""
    tunnel = session.get(AgentTunnel, agent_id)
    if tunnel is None:
        tunnel = AgentTunnel(agent_id=agent_id, public_key=public_key, tunnel_ip=_allocate(session))
        session.add(tunnel)
    tunnel.public_key = public_key
    tunnel.updated_at = _now()
    session.commit()
    return tunnel


def upsert_relay(session: Session, relay_id: str, endpoint: str, public_key: str):
    """Returns (relay, credential or None). The credential is shown only on first registration."""
    relay = session.get(RelayServer, relay_id)
    credential = None
    if relay is None:
        credential = "cmp_relay_" + secrets.token_urlsafe(32)
        relay = RelayServer(id=relay_id, credential_digest=_digest(credential))
        session.add(relay)
    relay.endpoint, relay.public_key, relay.updated_at = endpoint, public_key, _now()
    settings_row = session.get(TunnelSettings, 1)
    if settings_row is None:
        settings_row = TunnelSettings(id=1, config_version=0)
        session.add(settings_row)
    settings_row.config_version += 1
    session.commit()
    return relay, credential


def authenticate_relay(session: Session, relay_id: str, credential: str) -> RelayServer:
    """For the relay-side Agent list (P1, pending). Constant-time digest comparison."""
    relay = session.get(RelayServer, relay_id)
    if relay is None or not hmac.compare_digest(relay.credential_digest, _digest(credential)):
        raise TunnelError("RELAY_AUTH_FAILED")
    return relay


def exposed_ports(session: Session, agent_id: str) -> list[int]:
    """T5: PostgreSQL plus the app ports of this Agent's READY ON_PREM results."""
    ports = {DB_PORT}
    rows = session.scalars(
        select(ProvisioningResultRecord).where(ProvisioningResultRecord.status == "READY")
    )
    for row in rows:
        result = row.result
        cluster = result.get("cluster") or {}
        app = result.get("application") or {}
        mine = result.get("provider") == "ON_PREM" and cluster.get("agent_id") == agent_id
        if mine and app.get("port"):
            ports.add(int(app["port"]))
    return sorted(ports)


def agent_config(session: Session, agent_id: str) -> dict:
    tunnel = session.get(AgentTunnel, agent_id)
    if tunnel is None:
        raise TunnelError("TUNNEL_KEY_NOT_REGISTERED")
    relays = session.scalars(select(RelayServer).order_by(RelayServer.id))
    return {
        "tunnel_ip": tunnel.tunnel_ip,
        "relays": [
            {"relay_id": r.id, "endpoint": r.endpoint, "public_key": r.public_key} for r in relays
        ],
        "allowed_ips": [TUNNEL_CIDR],
        "mtu": MTU,
        "persistent_keepalive": PERSISTENT_KEEPALIVE,
        "exposed_ports": exposed_ports(session, agent_id),
        "config_version": config_version(session),
    }


def record_status(
    session: Session,
    agent_id: str,
    *,
    tunnel_ip: str,
    last_handshake_at: int | None,
    app_health_ok: bool,
    db_connect_ok: bool,
) -> None:
    tunnel = session.get(AgentTunnel, agent_id)
    if tunnel is None:
        raise TunnelError("TUNNEL_KEY_NOT_REGISTERED")
    if tunnel.tunnel_ip != tunnel_ip:
        raise TunnelError("TUNNEL_IP_MISMATCH")
    status = session.get(TunnelStatus, agent_id) or TunnelStatus(agent_id=agent_id)
    status.tunnel_ip, status.last_handshake_at = tunnel_ip, last_handshake_at
    status.app_health_ok, status.db_connect_ok = app_health_ok, db_connect_ok
    status.reported_at = _now()
    session.add(status)
    session.commit()


def tunnel_view(session: Session, agent_id: str | None) -> dict | None:
    """Latest tunnel check for the handoff (T6/T7); None when nothing was reported."""
    if not agent_id:
        return None
    status = session.get(TunnelStatus, agent_id)
    if status is None:
        return None
    return {
        "tunnel_ip": status.tunnel_ip,
        "app_health_ok": status.app_health_ok,
        "db_connect_ok": status.db_connect_ok,
        "last_handshake_at": status.last_handshake_at,
    }
