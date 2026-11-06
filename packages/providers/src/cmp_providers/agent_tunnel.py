"""On-premises Agent WireGuard tunnel (ticket 17, #71; decisions W1~W9).

The Agent is the WireGuard *client*: it connects outbound to the relay server (Traffic owns the
relay, its WireGuard server and Envoy) and exposes the app port and PostgreSQL 5432 only on its
tunnel IP, forwarding to the K3s Services. Ticket 73 (ADR-007 step 2): when the platform names a
`kube_api_port`, that port forwards to the K3s API (`kubernetes.default.svc:443`) for the data
worker's kubeconfig; the Agent only relays TCP (TLS and authentication stay end to end). Runs as the `tunnel` container of the Agent Pod, in the
Pod network namespace (no hostNetwork), with NET_ADMIN only.

Loop (W5~W7): register the public key → read the tunnel config → apply interface/peer when the
desired state changes → keep forwarders bound to the tunnel IP → check handshake, app health and
PostgreSQL through the tunnel address → report to the platform. The private key lives only in the
Agent-namespace Secret and reaches `kubectl`/`wg` on stdin; logs and reports carry codes only.
"""

import argparse
import base64
import binascii
import http.client
import ipaddress
import json
import os
import re
import secrets
import selectors
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cmp_providers.agent_runtime import halt, read_wake, write_wake  # noqa: F401
from cmp_providers.onprem_agent import (
    ENVIRONMENT,
    MANAGED,
    ClusterTarget,
    ClusterUnavailable,
    RolloutAgentError,
)
from cmp_providers.remote_agent import AgentControlClient, AgentTransportError

INTERFACE = "wg0"
DB_PORT = 5432
KUBE_API_HOST = "kubernetes.default.svc"  # ticket 73: in-cluster DNS of `default/kubernetes`
KUBE_API_TARGET_PORT = 443
STALE_AFTER = 180  # WireGuard Reject-After-Time: no handshake for this long means no session
UNAUTHORIZED_EXIT = 2
KEY_SECRET = "cmp-agent-wireguard"
# Ticket 66 (#283): after a wake-up (start, Agent job signal, exposed-port change) an unhealthy
# tunnel re-checks every FAST_INTERVAL seconds for at most FAST_WINDOW seconds.
FAST_INTERVAL = 2.0
FAST_WINDOW = 120.0
WAKE_TICK = 1.0
_ENDPOINT = re.compile(r"^[A-Za-z0-9.-]{1,253}:[0-9]{1,5}$")


class TunnelAgentError(Exception):
    """Code-only tunnel failure (never keys, endpoints or command output)."""


# --- X25519 (RFC 7748) — derives the WireGuard public key without extra dependencies (W4) -----

_P = 2**255 - 19
_A24 = 121665


def _clamp(scalar: bytes) -> int:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(k, "little")


def _x25519(scalar: bytes, u: int) -> int:
    k, x1, x2, z2, x3, z3, swap = _clamp(scalar), u, 1, 0, u, 1, 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return x2 * pow(z2, _P - 2, _P) % _P


def generate_private_key() -> bytes:
    key = bytearray(secrets.token_bytes(32))
    key[0] &= 248
    key[31] = (key[31] & 127) | 64
    return bytes(key)


def public_key(private_key: bytes) -> bytes:
    return _x25519(private_key, 9).to_bytes(32, "little")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _wg_key(value) -> str:
    try:
        if isinstance(value, str) and len(base64.b64decode(value, validate=True)) == 32:
            return value
    except (binascii.Error, ValueError):
        pass
    raise TunnelAgentError("INVALID_TUNNEL_CONFIG")


# --- private key store ------------------------------------------------------------------------


class MemoryTunnelKeyStore:
    def __init__(self, key: bytes | None = None):
        self.key = key

    def load(self) -> bytes | None:
        return self.key

    def save(self, key: bytes) -> None:
        self.key = key


class SecretTunnelKeyStore:
    """W4: Helm-unmanaged Secret in the Agent Namespace; written via `kubectl apply -f -`."""

    def __init__(self, target, *, namespace: str, name: str = KEY_SECRET):
        self.target, self.namespace, self.name = target, namespace, name

    def load(self) -> bytes | None:
        secret = self.target.get("secret", self.name, self.namespace)
        encoded = ((secret or {}).get("data") or {}).get("private_key")
        if not encoded:
            return None
        key = base64.b64decode(base64.b64decode(encoded).decode().strip())
        return key if len(key) == 32 else None

    def save(self, key: bytes) -> None:
        self.target.apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {
                    "name": self.name,
                    "namespace": self.namespace,
                    "labels": {MANAGED: "true"},
                },
                "data": {"private_key": _b64(_b64(key).encode())},
            }
        )


# --- desired state ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TunnelPlan:
    tunnel_ip: str
    relay_ip: str
    relay_allowed_ips: str
    relay_endpoint: str
    relay_public_key: str
    mtu: int
    keepalive: int
    config_version: int


def plan_from_config(config: dict) -> TunnelPlan:
    """W3/W5: first relay by id; peer AllowedIPs narrowed to the relay's tunnel address."""
    relays = sorted(config.get("relays") or [], key=lambda r: str(r.get("relay_id")))
    if not relays:
        raise TunnelAgentError("RELAY_NOT_CONFIGURED")
    relay = relays[0]
    try:
        network = ipaddress.IPv4Network((config.get("allowed_ips") or [None])[0])
        tunnel_ip = ipaddress.IPv4Address(config["tunnel_ip"])
        mtu, keepalive = int(config["mtu"]), int(config["persistent_keepalive"])
        version = int(config["config_version"])
    except (KeyError, TypeError, ValueError):
        raise TunnelAgentError("INVALID_TUNNEL_CONFIG") from None
    relay_ip = network.network_address + 1
    if (
        tunnel_ip not in network
        or tunnel_ip in (relay_ip, network.network_address, network.broadcast_address)
        or not 1280 <= mtu <= 1500
        or not 0 <= keepalive <= 65535
        or not _ENDPOINT.match(str(relay.get("endpoint")))
    ):
        raise TunnelAgentError("INVALID_TUNNEL_CONFIG")
    return TunnelPlan(
        tunnel_ip=str(tunnel_ip),
        relay_ip=str(relay_ip),
        relay_allowed_ips=f"{relay_ip}/32",
        relay_endpoint=relay["endpoint"],
        relay_public_key=_wg_key(relay.get("public_key")),
        mtu=mtu,
        keepalive=keepalive,
        config_version=version,
    )


# --- forward targets (W2) ---------------------------------------------------------------------


@dataclass(frozen=True)
class Forward:
    port: int  # listening port on the tunnel IP (same number as the Service port, P2)
    host: str  # Service DNS name
    target_port: int
    health_path: str | None  # None for PostgreSQL


class KubeView:
    """Read-only list/get on top of the Agent's explicit-context `ClusterTarget`."""

    def __init__(self, target: ClusterTarget):
        self.target = target

    def list(self, kind: str, namespace: str | None = None) -> list[dict]:
        args = ["get", kind, "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        result = self.target.kubectl(*args)
        if result.returncode:
            raise ClusterUnavailable()
        return json.loads(result.stdout).get("items", [])

    def get(self, kind: str, name: str, namespace: str | None = None) -> dict | None:
        return self.target.get(kind, name, namespace)


def _health_path(deployment: dict | None) -> str:
    containers = (((deployment or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    for container in containers.get("containers") or []:
        path = ((container.get("readinessProbe") or {}).get("httpGet") or {}).get("path")
        if isinstance(path, str) and path.startswith("/"):
            return path
    return "/health"


def discover_forwards(view, ports: list[int]) -> tuple[dict[int, Forward], list[str]]:
    """Services in Agent-owned Namespaces (`cmp.dev/environment-id`) matched by port."""
    owned = [
        n["metadata"]["name"]
        for n in view.list("namespaces")
        if ((n.get("metadata") or {}).get("annotations") or {}).get(ENVIRONMENT)
    ]
    candidates: dict[int, list[tuple[str, str]]] = {}
    for namespace in owned:
        for service in view.list("services", namespace):
            for entry in (service.get("spec") or {}).get("ports") or []:
                candidates.setdefault(entry.get("port"), []).append(
                    (service["metadata"]["name"], namespace)
                )
    forwards, codes = {}, []
    for port in sorted(set(ports)):
        found = candidates.get(port, [])
        if len(found) != 1:
            codes.append("FORWARD_TARGET_MISSING" if not found else "FORWARD_TARGET_AMBIGUOUS")
            continue
        name, namespace = found[0]
        path = None if port == DB_PORT else _health_path(view.get("deployment", name, namespace))
        forwards[port] = Forward(port, f"{name}.{namespace}.svc", port, path)
    return forwards, codes


# --- probes (W7) ------------------------------------------------------------------------------


def http_health_ok(ip: str, port: int, path: str, timeout: float = 5) -> bool:
    connection = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"User-Agent": "cmp-onprem-agent"})
        return 200 <= connection.getresponse().status < 300
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


_SSL_REQUEST = struct.pack("!II", 8, 80877103)


def postgres_ready(ip: str, port: int = DB_PORT, timeout: float = 5) -> bool:
    """Same depth as `pg_isready`: a PostgreSQL server answers SSLRequest with `S` or `N`."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as connection:
            connection.sendall(_SSL_REQUEST)
            return connection.recv(1) in (b"S", b"N")
    except OSError:
        return False


def tcp_connect_ok(ip: str, port: int, timeout: float = 5) -> bool:
    """Ticket 73: the K3s API port accepts a TCP connection through the tunnel address."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def kube_api_port(config: dict) -> int | None:
    """Ticket 73: the configured K3s API port, None when absent or unusable."""
    value = config.get("kube_api_port")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 65535 and value != DB_PORT else None


# --- forwarders (W2/W3) -----------------------------------------------------------------------


class _Listener:
    def __init__(self, bind_ip: str, forward: Forward, allowed: frozenset[str]):
        self.forward, self.allowed = forward, allowed
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((bind_ip, forward.port))  # tunnel IP only: never 0.0.0.0
        self.socket.listen(64)
        self.closed = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                client, (source, _) = self.socket.accept()
            except OSError:
                return
            if source not in self.allowed:
                client.close()
                continue
            threading.Thread(target=self._pipe, args=(client,), daemon=True).start()

    def _pipe(self, client: socket.socket):
        try:
            upstream = socket.create_connection(
                (self.forward.host, self.forward.target_port), timeout=10
            )
        except OSError:
            client.close()
            return
        upstream.settimeout(None)
        client.settimeout(None)
        selector = selectors.DefaultSelector()
        selector.register(client, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, client)
        try:
            while not self.closed.is_set():
                for key, _ in selector.select(timeout=1):
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)
        except OSError:
            return
        finally:
            selector.close()
            client.close()
            upstream.close()

    def close(self):
        self.closed.set()
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class TcpForwarders:
    """Keeps one listener per exposed port on the tunnel IP; reconciles on `apply`."""

    def __init__(self):
        self.listeners: dict[int, tuple[tuple, _Listener]] = {}

    def apply(self, bind_ip: str, allowed_sources, forwards: dict[int, Forward]) -> None:
        allowed = frozenset(allowed_sources)
        for port in list(self.listeners):
            spec, listener = self.listeners[port]
            if spec != (bind_ip, allowed, forwards.get(port)):
                listener.close()
                del self.listeners[port]
        for port, forward in forwards.items():
            if port not in self.listeners:
                try:
                    listener = _Listener(bind_ip, forward, allowed)
                except OSError:
                    raise TunnelAgentError("FORWARDER_BIND_FAILED") from None
                self.listeners[port] = ((bind_ip, allowed, forward), listener)

    def close(self) -> None:
        for _, listener in self.listeners.values():
            listener.close()
        self.listeners.clear()


# --- WireGuard link (W1) ----------------------------------------------------------------------


def _run(argv: list[str], content: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=content, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 127, b"", b"")


def _spawn(argv: list[str], env: dict):
    return subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WireGuardLink:
    """Kernel WireGuard via `ip`/`wg`; `wireguard-go` (userspace) when the kernel lacks it."""

    def __init__(
        self,
        *,
        run: Callable = _run,
        spawn: Callable = _spawn,
        mode: str = "auto",
        name: str = INTERFACE,
        wait: Callable[[float], None] = time.sleep,
    ):
        if mode not in {"auto", "kernel", "userspace"}:
            raise TunnelAgentError("INVALID_TUNNEL_MODE")
        self.run, self.spawn, self.mode, self.name, self.wait = run, spawn, mode, name, wait
        self.process = None
        self.mode_in_use: str | None = None

    def _ok(self, *argv: str, content: bytes | None = None) -> bool:
        return self.run(list(argv), content).returncode == 0

    def _must(self, *argv: str, content: bytes | None = None) -> None:
        if not self._ok(*argv, content=content):
            raise TunnelAgentError("TUNNEL_INTERFACE_FAILED")

    def exists(self) -> bool:
        return self._ok("ip", "link", "show", "dev", self.name)

    def _create(self) -> None:
        if self.mode != "userspace" and self._ok(
            "ip", "link", "add", "dev", self.name, "type", "wireguard"
        ):
            self.mode_in_use = "kernel"
            return
        if self.mode == "kernel":
            raise TunnelAgentError("TUNNEL_INTERFACE_FAILED")
        env = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")}
        env["WG_PROCESS_FOREGROUND"] = "1"
        try:
            self.process = self.spawn(["wireguard-go", self.name], env)
        except OSError:
            raise TunnelAgentError("TUNNEL_INTERFACE_FAILED") from None
        for _ in range(50):
            if self.exists():
                self.mode_in_use = "userspace"
                return
            self.wait(0.1)
        raise TunnelAgentError("TUNNEL_INTERFACE_FAILED")

    def ensure(self, plan: TunnelPlan, private_key: bytes) -> None:
        if not self.exists():
            self._create()
        self._must(
            "wg",
            "set",
            self.name,
            "private-key",
            "/dev/stdin",
            content=_b64(private_key).encode() + b"\n",
        )
        self._must("ip", "address", "flush", "dev", self.name)
        self._must("ip", "address", "add", f"{plan.tunnel_ip}/32", "dev", self.name)
        self._must("ip", "link", "set", "dev", self.name, "mtu", str(plan.mtu), "up")
        self._must("ip", "route", "replace", plan.relay_allowed_ips, "dev", self.name)

    def set_peer(self, plan: TunnelPlan) -> None:
        peers = self.run(["wg", "show", self.name, "peers"], None)
        for peer in peers.stdout.decode().split():
            if peer != plan.relay_public_key:
                self._must("wg", "set", self.name, "peer", peer, "remove")
        self._must(
            "wg",
            "set",
            self.name,
            "peer",
            plan.relay_public_key,
            "endpoint",
            plan.relay_endpoint,
            "allowed-ips",
            plan.relay_allowed_ips,
            "persistent-keepalive",
            str(plan.keepalive),
        )

    def latest_handshake(self, relay_public_key: str) -> int | None:
        result = self.run(["wg", "show", self.name, "latest-handshakes"], None)
        for line in result.stdout.decode().splitlines():
            peer, _, seconds = line.partition("\t")
            if peer == relay_public_key and seconds.strip().isdigit() and int(seconds):
                return int(seconds)
        return None

    def down(self) -> None:
        self.run(["ip", "link", "del", "dev", self.name], None)
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.process = None


# --- manager (W5~W8) --------------------------------------------------------------------------


class TunnelManager:
    def __init__(
        self,
        *,
        agent_id: str,
        credential: str,
        client,
        keys,
        link,
        forwarders,
        discover: Callable[[list[int]], tuple[dict[int, Forward], list[str]]],
        http_ok: Callable[[str, int, str], bool] = http_health_ok,
        db_ok: Callable[[str, int], bool] = postgres_ready,
        tcp_ok: Callable[[str, int], bool] = tcp_connect_ok,
        expose_kube_api: bool = False,
        clock: Callable[[], float] = time.time,
        log: Callable[[dict], None] = lambda event: None,
        stale_after: int = STALE_AFTER,
    ):
        self.agent_id, self.credential, self.client = agent_id, credential, client
        self.keys, self.link, self.forwarders, self.discover = keys, link, forwarders, discover
        self.http_ok, self.db_ok, self.clock, self.log = http_ok, db_ok, clock, log
        # Ticket 73: the cluster owner's chart value; without it `kube_api_port` is ignored.
        self.tcp_ok, self.expose_kube_api = tcp_ok, expose_kube_api
        self.stale_after = stale_after
        self.private_key: bytes | None = None
        self.applied: TunnelPlan | None = None
        self.last_action = 0.0
        self.recoveries = 0
        self.last_event: dict | None = None
        self.ports: list[int] | None = None  # exposed ports of the latest config

    def start(self) -> None:
        """Load or create the private key, then register its public key (idempotent)."""
        key = self.keys.load()
        if key is None:
            key = generate_private_key()
            self.keys.save(key)
            self.log({"event": "tunnel_key_created"})
        self.private_key = key
        self.client.register_tunnel_key(
            agent_id=self.agent_id, credential=self.credential, public_key=_b64(public_key(key))
        )

    def _apply(self, plan: TunnelPlan) -> None:
        self.link.ensure(plan, self.private_key)
        self.link.set_peer(plan)
        self.applied, self.last_action, self.recoveries = plan, self.clock(), 0
        mode = getattr(self.link, "mode_in_use", None)
        self.log({"event": "tunnel_applied", "config_version": plan.config_version, "mode": mode})

    def _recover(self, plan: TunnelPlan) -> None:
        """W6: first re-set the peer (endpoint DNS re-resolved), then recreate the interface."""
        self.recoveries += 1
        self.last_action = self.clock()
        if self.recoveries == 1:
            self.link.set_peer(plan)
            self.log({"event": "tunnel_recover", "action": "PEER_RESET"})
        else:
            self.link.down()
            self.forwarders.close()
            self._apply(plan)
            self.log({"event": "tunnel_recover", "action": "INTERFACE_RECREATED"})

    def _report(self, tunnel_ip, handshake, app_ok, db_ok, code, kube_ok=None) -> dict:
        report = {
            "tunnel_ip": tunnel_ip,
            "last_handshake_at": handshake,
            "app_health_ok": app_ok,
            "db_connect_ok": db_ok,
            "code": code,
        }
        if kube_ok is not None:  # ticket 73: only sent when exposed (older platforms forbid it)
            report["kube_api_ok"] = kube_ok
        self.client.report_tunnel_status(
            agent_id=self.agent_id, credential=self.credential, report=report
        )
        event = {"event": "tunnel", "code": code, "app_health_ok": app_ok, "db_connect_ok": db_ok}
        if kube_ok is not None:
            event["kube_api_ok"] = kube_ok
        if event != self.last_event:  # log changes only
            self.log(event | {"last_handshake_at": handshake})
            self.last_event = event
        return report

    def step(self) -> dict:
        if self.private_key is None:
            self.start()
        config = self.client.tunnel_config(agent_id=self.agent_id, credential=self.credential)
        tunnel_ip = str(config.get("tunnel_ip"))
        try:
            plan = plan_from_config(config)
        except TunnelAgentError as exc:
            return self._report(tunnel_ip, None, False, False, str(exc))
        try:
            if plan != self.applied or not self.link.exists():
                self._apply(plan)
        except TunnelAgentError as exc:
            self.applied = None
            return self._report(tunnel_ip, None, False, False, str(exc))

        ports = sorted({int(p) for p in config.get("exposed_ports") or []})
        self.ports = ports
        # Never an app Service port, even when this cluster's owner keeps it closed.
        named = kube_api_port(config)
        service_ports = [p for p in ports if p != named]
        kube = named if self.expose_kube_api else None
        try:
            forwards, codes = self.discover(service_ports)
        except RolloutAgentError:
            forwards, codes = {}, ["KUBERNETES_UNAVAILABLE"]
        if kube is not None:
            forwards = forwards | {kube: Forward(kube, KUBE_API_HOST, KUBE_API_TARGET_PORT, None)}
        try:
            self.forwarders.apply(plan.tunnel_ip, {plan.relay_ip, plan.tunnel_ip}, forwards)
        except TunnelAgentError as exc:
            forwards, codes = {}, [str(exc), *codes]

        now = self.clock()
        handshake = self.link.latest_handshake(plan.relay_public_key)
        fresh = handshake is not None and now - handshake <= self.stale_after
        if fresh:
            self.recoveries = 0
        elif now - self.last_action > self.stale_after:
            try:
                self._recover(plan)
            except TunnelAgentError:
                self.applied = None

        app_ports = [p for p in service_ports if p != DB_PORT]
        app_ok = bool(app_ports) and all(
            p in forwards and self.http_ok(plan.tunnel_ip, p, forwards[p].health_path)
            for p in app_ports
        )
        db_ok = DB_PORT in forwards and self.db_ok(plan.tunnel_ip, DB_PORT)
        kube_ok = None
        if kube is not None:  # diagnostics only: never changes `code` (ticket 73)
            kube_ok = fresh and kube in forwards and self.tcp_ok(plan.tunnel_ip, kube)
        if not fresh:
            code = "HANDSHAKE_MISSING"
        elif codes:
            code = codes[0]
        elif not app_ports:
            code = "APP_PORT_NOT_ASSIGNED"
        elif not app_ok:
            code = "APP_HEALTH_FAILED"
        elif not db_ok:
            code = "DB_CONNECT_FAILED"
        else:
            code = None
        return self._report(
            plan.tunnel_ip, handshake, fresh and app_ok, fresh and db_ok, code, kube_ok
        )

    def shutdown(self) -> None:
        self.forwarders.close()
        self.link.down()
        self.applied = None


def run_tunnel(
    manager: TunnelManager,
    *,
    wait: Callable[[float], bool],
    interval: float,
    log: Callable[[dict], None],
    max_backoff: float = 60,
    wake: Callable[[], object] | None = None,
    fast_interval: float = FAST_INTERVAL,
    fast_window: float = FAST_WINDOW,
    tick: float = WAKE_TICK,
) -> int:
    """`wait(seconds)` returns True when a stop was requested. On revocation (W9) the tunnel is
    taken down and the loop idles until a stop (ticket 52, D14), then exits with code 2.

    Ticket 66 (#283): start-up, a new `wake()` token (the `agent` container finished a job) and
    an exposed-port change open a fast window. Inside it an unhealthy report is followed by the
    next check after `fast_interval`; a healthy report, or the end of the window, returns to
    `interval`. Checks are never closer than `fast_interval`, so one window costs at most
    `fast_window / fast_interval` extra config/status call pairs."""
    clock = manager.clock
    failures = 0
    fast_until = clock() + fast_window
    token = wake() if wake else None
    ports = None
    try:
        while True:
            try:
                report = manager.step()
                failures = 0
                if ports is not None and manager.ports != ports:
                    fast_until = clock() + fast_window
                ports = manager.ports
                healthy = (report or {}).get("code") is None
                fast = not healthy and clock() < fast_until
                delay = fast_interval if fast else interval
            except AgentTransportError as exc:
                if str(exc) == "AGENT_UNAUTHORIZED":
                    log({"event": "stopped", "code": "AGENT_UNAUTHORIZED"})
                    manager.shutdown()
                    return halt(wait)
                delay = min(2**failures, max_backoff)
                failures += 1
                log({"event": "retry", "code": str(exc), "seconds": delay})
            except RolloutAgentError as exc:
                delay = min(2**failures, max_backoff)
                failures += 1
                log({"event": "retry", "code": exc.code, "seconds": delay})
            if wake is None:
                if wait(delay):
                    return 0
                continue
            checked = clock()
            due = checked + delay
            while (remaining := due - clock()) > 0:
                if wait(min(tick, remaining)):
                    return 0
                current = wake()
                if current != token:
                    token = current
                    fast_until = clock() + fast_window
                    if not failures:  # platform outages keep their backoff
                        due = min(due, checked + fast_interval)
    finally:
        manager.shutdown()


def _log(event: dict) -> None:
    print(json.dumps(event, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    from cmp_providers.agent_runtime import SecretCredentialStore

    parser = argparse.ArgumentParser(description="On-premises Agent WireGuard tunnel")
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--namespace", default="cmp-system")
    parser.add_argument("--mode", choices=["auto", "kernel", "userspace"], default="auto")
    parser.add_argument("--interval-seconds", type=float, default=15)
    parser.add_argument("--wake-file", help="signal file the agent container writes (ticket 66)")
    parser.add_argument(
        "--expose-kube-api",
        action="store_true",
        help="forward the platform's kube_api_port to kubernetes.default.svc:443 (ticket 73)",
    )
    args = parser.parse_args(argv)

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        client = AgentControlClient(args.platform_url)
        target = ClusterTarget.in_cluster()
        link = WireGuardLink(mode=args.mode)
    except (AgentTransportError, RolloutAgentError, TunnelAgentError) as exc:
        _log({"event": "stopped", "code": str(exc)})
        return UNAUTHORIZED_EXIT

    # The `agent` container registers and stores the credential (15); wait for it.
    credentials = SecretCredentialStore(target, namespace=args.namespace)
    failures, credential = 0, None
    while credential is None:
        try:
            credential = credentials.load_credential()
        except RolloutAgentError:
            credential = None
        if credential is None:
            delay = min(2**failures, 30)
            failures += 1
            _log({"event": "waiting", "code": "AGENT_CREDENTIAL_PENDING", "seconds": delay})
            if stop.wait(delay):
                return 0

    view = KubeView(target)
    manager = TunnelManager(
        agent_id=args.agent_id,
        credential=credential,
        client=client,
        keys=SecretTunnelKeyStore(target, namespace=args.namespace),
        link=link,
        forwarders=TcpForwarders(),
        discover=lambda ports: discover_forwards(view, ports),
        log=_log,
        expose_kube_api=args.expose_kube_api,
    )
    wake = (lambda: read_wake(args.wake_file)) if args.wake_file else None
    return run_tunnel(manager, wait=stop.wait, interval=args.interval_seconds, log=_log, wake=wake)


if __name__ == "__main__":
    sys.exit(main())
