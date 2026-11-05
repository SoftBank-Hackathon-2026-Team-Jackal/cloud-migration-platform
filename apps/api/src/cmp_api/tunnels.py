"""Tunnel configuration exchange between on-prem Agents and relay servers (ticket 16).

The platform only relays *public* WireGuard material: Agent public keys and tunnel IPs one way
(relay polling, P1/T12), relay endpoints and public keys the other way. Private keys never reach
any API. Results stay immutable (13 R3); tunnel checks are merged into the handoff view at read
time (T6).
"""

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from ipaddress import IPv4Network

from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from cmp_api.agent_registry import RegisteredAgent
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.provisioning_results import ProvisioningResultRecord

TUNNEL_CIDR = "10.200.0.0/24"  # T4, confirmed by the Traffic owner (P3)
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
    code: Mapped[str | None] = mapped_column(String(64))  # ticket 17 W8 failure code
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
    """Relay credential check (T3) for the relay-side Agent list. Constant-time comparison."""
    relay = session.get(RelayServer, relay_id)
    if relay is None or not hmac.compare_digest(relay.credential_digest, _digest(credential)):
        raise TunnelError("RELAY_AUTH_FAILED")
    return relay


def _app_ports(session: Session) -> dict[str, set[int]]:
    """App ports of READY ON_PREM results, grouped by the Agent that runs them."""
    ports: dict[str, set[int]] = {}
    rows = session.scalars(
        select(ProvisioningResultRecord).where(ProvisioningResultRecord.status == "READY")
    )
    for row in rows:
        result = row.result
        agent_id = (result.get("cluster") or {}).get("agent_id")
        app = result.get("application") or {}
        if result.get("provider") == "ON_PREM" and agent_id and app.get("port"):
            ports.setdefault(agent_id, set()).add(int(app["port"]))
    return ports


def exposed_ports(session: Session, agent_id: str) -> list[int]:
    """T5: PostgreSQL plus the app ports of this Agent's READY ON_PREM results."""
    return sorted({DB_PORT} | _app_ports(session).get(agent_id, set()))


def relay_peers(session: Session) -> list[dict]:
    relays = session.scalars(select(RelayServer).order_by(RelayServer.id))
    return [{"relay_id": r.id, "endpoint": r.endpoint, "public_key": r.public_key} for r in relays]


def relay_agent_list(session: Session) -> dict:
    """P1/T12: every Agent with a tunnel key, for relay polling.

    Revoked Agents stay listed with `revoked: true` and no ports so the relay can remove the peer.
    `list_version` is a digest of the list itself, so any change (new Agent, key rotation,
    revocation, port change) yields a new value without extra bookkeeping.
    """
    app_ports = _app_ports(session)
    rows = session.execute(
        select(AgentTunnel, RegisteredAgent.revoked_at)
        .outerjoin(RegisteredAgent, RegisteredAgent.id == AgentTunnel.agent_id)
        .order_by(AgentTunnel.agent_id)
    )
    agents = []
    for tunnel, revoked_at in rows:
        revoked = revoked_at is not None
        ports = [] if revoked else sorted({DB_PORT} | app_ports.get(tunnel.agent_id, set()))
        agents.append(
            {
                "agent_id": tunnel.agent_id,
                "public_key": tunnel.public_key,
                "tunnel_ip": tunnel.tunnel_ip,
                "allowed_ips": [f"{tunnel.tunnel_ip}/32"],
                "allowed_ports": ports,
                "revoked": revoked,
            }
        )
    canonical = json.dumps(agents, sort_keys=True, separators=(",", ":"))
    return {"list_version": _digest(canonical)[:16], "agents": agents}


def agent_config(session: Session, agent_id: str) -> dict:
    tunnel = session.get(AgentTunnel, agent_id)
    if tunnel is None:
        raise TunnelError("TUNNEL_KEY_NOT_REGISTERED")
    return {
        "tunnel_ip": tunnel.tunnel_ip,
        "relays": relay_peers(session),
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
    code: str | None = None,
) -> None:
    tunnel = session.get(AgentTunnel, agent_id)
    if tunnel is None:
        raise TunnelError("TUNNEL_KEY_NOT_REGISTERED")
    if tunnel.tunnel_ip != tunnel_ip:
        raise TunnelError("TUNNEL_IP_MISMATCH")
    status = session.get(TunnelStatus, agent_id) or TunnelStatus(agent_id=agent_id)
    status.tunnel_ip, status.last_handshake_at = tunnel_ip, last_handshake_at
    status.app_health_ok, status.db_connect_ok = app_health_ok, db_connect_ok
    status.code = code
    status.reported_at = _now()
    session.add(status)
    session.commit()


def tunnel_freshness(status: TunnelStatus, *, now: int) -> tuple[bool, int | None]:
    """Ticket 25 F1: (stale, fresh_until) of a tunnel report at `now` (API server clock).

    Fresh only while the report itself is recent (Agent and API alive) and the handshake it is
    based on is recent (tunnel alive). Without a handshake the report is never current evidence.
    """
    if status.last_handshake_at is None:
        return True, None
    fresh_until = min(
        status.reported_at + settings.tunnel_status_ttl_seconds,
        status.last_handshake_at + settings.tunnel_handshake_stale_seconds,
    )
    return now > fresh_until, fresh_until


def tunnel_view(session: Session, agent_id: str | None, *, now: int | None = None) -> dict | None:
    """Latest tunnel check for the handoff (T6/T7, 25 F5); None when nothing was reported."""
    if not agent_id:
        return None
    status = session.get(TunnelStatus, agent_id)
    if status is None:
        return None
    stale, fresh_until = tunnel_freshness(status, now=_now() if now is None else now)
    return {
        "tunnel_ip": status.tunnel_ip,
        "app_health_ok": status.app_health_ok,
        "db_connect_ok": status.db_connect_ok,
        "last_handshake_at": status.last_handshake_at,
        "code": status.code,
        "stale": stale,
        "fresh_until": fresh_until,
    }
