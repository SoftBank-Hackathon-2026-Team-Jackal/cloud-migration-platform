"""Ticket 17 (#71) seam: the Agent-side WireGuard tunnel manager with fakes for the platform,
the WireGuard link and probes; real loopback sockets for the forwarder and probes.

Real WireGuard, k3d and a test relay peer are covered by `scripts/smoke_onprem_agent_tunnel.py`.
"""

import base64
import io
import json
import socket
import subprocess
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from itertools import pairwise

import pytest
from cmp_providers.agent_runtime import HALT_SECONDS
from cmp_providers.agent_tunnel import (
    Forward,
    MemoryTunnelKeyStore,
    SecretTunnelKeyStore,
    TcpForwarders,
    TunnelAgentError,
    TunnelManager,
    WireGuardLink,
    discover_forwards,
    generate_private_key,
    http_health_ok,
    plan_from_config,
    postgres_ready,
    public_key,
    read_wake,
    run_tunnel,
    write_wake,
)
from cmp_providers.remote_agent import AgentControlClient, AgentTransportError

RELAY_KEY = base64.b64encode(b"r" * 32).decode()
RELAY_KEY_2 = base64.b64encode(b"s" * 32).decode()


def config(**overrides) -> dict:
    return {
        "tunnel_ip": "10.200.0.2",
        "relays": [
            {"relay_id": "relay-seoul", "endpoint": "13.124.10.10:51820", "public_key": RELAY_KEY}
        ],
        "allowed_ips": ["10.200.0.0/24"],
        "mtu": 1420,
        "persistent_keepalive": 25,
        "exposed_ports": [5432, 8080],
        "config_version": 1,
    } | overrides


# --- X25519 (W4) ------------------------------------------------------------------------------


def test_public_key_matches_the_rfc7748_test_vector():
    alice = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    expected = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    assert public_key(alice).hex() == expected


def test_generated_private_key_is_clamped_and_random():
    first, second = generate_private_key(), generate_private_key()
    assert len(first) == 32 and first != second
    assert first[0] & 7 == 0 and first[31] & 0x80 == 0 and first[31] & 0x40


# --- desired state (W3/W5) -------------------------------------------------------------------


def test_plan_narrows_allowed_ips_to_the_relay_tunnel_address():
    plan = plan_from_config(config())
    assert plan.tunnel_ip == "10.200.0.2"
    assert plan.relay_ip == "10.200.0.1"
    assert plan.relay_allowed_ips == "10.200.0.1/32"
    assert (plan.relay_endpoint, plan.relay_public_key) == ("13.124.10.10:51820", RELAY_KEY)
    assert (plan.mtu, plan.keepalive, plan.config_version) == (1420, 25, 1)


def test_plan_uses_the_first_relay_by_id_and_requires_one():
    relays = [
        {"relay_id": "relay-z", "endpoint": "z.example:51820", "public_key": RELAY_KEY_2},
        {"relay_id": "relay-a", "endpoint": "a.example:51820", "public_key": RELAY_KEY},
    ]
    assert plan_from_config(config(relays=relays)).relay_endpoint == "a.example:51820"
    with pytest.raises(TunnelAgentError, match="RELAY_NOT_CONFIGURED"):
        plan_from_config(config(relays=[]))


@pytest.mark.parametrize(
    "bad",
    [
        {"tunnel_ip": "192.168.0.2"},  # outside the tunnel CIDR
        {"tunnel_ip": "10.200.0.1"},  # the relay address
        {"mtu": 9000},
        {"relays": [{"relay_id": "r", "endpoint": "x y:1", "public_key": RELAY_KEY}]},
        {"relays": [{"relay_id": "r", "endpoint": "a.example:1", "public_key": "short"}]},
    ],
)
def test_plan_rejects_an_unusable_config(bad):
    with pytest.raises(TunnelAgentError, match="INVALID_TUNNEL_CONFIG"):
        plan_from_config(config(**bad))


# --- manager with fakes ----------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.config = config()
        self.keys, self.reports = [], []
        self.fail_config = None

    def register_tunnel_key(self, *, agent_id, credential, public_key):
        self.keys.append(public_key)
        return {"tunnel_ip": "10.200.0.2", "relays": self.config["relays"], "config_version": 1}

    def tunnel_config(self, *, agent_id, credential):
        if self.fail_config:
            raise AgentTransportError(self.fail_config)
        return self.config

    def report_tunnel_status(self, *, agent_id, credential, report):
        self.reports.append(report)


class FakeLink:
    def __init__(self):
        self.calls, self.handshake, self.up = [], None, False
        self.private_keys = []

    def ensure(self, plan, private_key):
        self.calls.append(("ensure", plan.config_version))
        self.private_keys.append(private_key)
        self.up = True

    def set_peer(self, plan):
        self.calls.append(("peer", plan.relay_public_key, plan.relay_endpoint))

    def exists(self):
        return self.up

    def latest_handshake(self, relay_public_key):
        return self.handshake

    def down(self):
        self.calls.append(("down",))
        self.up = False


class FakeForwarders:
    def __init__(self):
        self.applied = []

    def apply(self, bind_ip, allowed_sources, forwards):
        self.applied.append((bind_ip, frozenset(allowed_sources), dict(forwards)))

    def close(self):
        self.applied.append(None)


FORWARDS = {
    5432: Forward(5432, "demo-db.demo.svc", 5432, None),
    8080: Forward(8080, "demo-app.demo.svc", 8080, "/health"),
}


class Clock:
    def __init__(self, now=1_000_000):
        self.now = now

    def __call__(self):
        return self.now


def manager(**overrides):
    parts = {
        "client": FakeClient(),
        "keys": MemoryTunnelKeyStore(),
        "link": FakeLink(),
        "forwarders": FakeForwarders(),
        "discover": lambda ports: ({p: FORWARDS[p] for p in ports if p in FORWARDS}, []),
        "http_ok": lambda ip, port, path: True,
        "db_ok": lambda ip, port: True,
        "clock": Clock(),
        "log": lambda event: None,
    } | overrides
    built = TunnelManager(agent_id="agent-001", credential="cmp_agent_secret", **parts)
    return built, parts


def test_key_is_generated_once_kept_in_the_store_and_only_the_public_key_is_registered():
    built, parts = manager()
    built.start()
    stored = parts["keys"].load()
    assert stored is not None
    assert parts["client"].keys == [base64.b64encode(public_key(stored)).decode()]

    again, _ = manager(keys=parts["keys"], client=parts["client"])
    again.start()
    assert parts["keys"].load() == stored  # restart reuses the Secret
    assert parts["client"].keys[0] == parts["client"].keys[1]


def test_step_brings_the_tunnel_up_binds_forwarders_to_the_tunnel_ip_and_reports_ok():
    built, parts = manager()
    parts["link"].handshake = parts["clock"].now - 10
    built.start()
    report = built.step()
    assert parts["link"].calls[:2] == [
        ("ensure", 1),
        ("peer", RELAY_KEY, "13.124.10.10:51820"),
    ]
    bind_ip, sources, forwards = parts["forwarders"].applied[-1]
    assert bind_ip == "10.200.0.2"
    assert sources == {"10.200.0.1", "10.200.0.2"}
    assert forwards == FORWARDS
    assert report == {
        "tunnel_ip": "10.200.0.2",
        "last_handshake_at": parts["clock"].now - 10,
        "app_health_ok": True,
        "db_connect_ok": True,
        "code": None,
    }
    assert parts["client"].reports == [report]


def test_unchanged_config_is_not_reapplied_and_a_new_version_is():
    built, parts = manager()
    parts["link"].handshake = parts["clock"].now
    built.start()
    built.step()
    built.step()
    assert [c[0] for c in parts["link"].calls] == ["ensure", "peer"]

    relays = [{"relay_id": "relay-seoul", "endpoint": "3.3.3.3:443", "public_key": RELAY_KEY_2}]
    parts["client"].config = config(relays=relays, config_version=2)
    built.step()
    assert parts["link"].calls[-2:] == [("ensure", 2), ("peer", RELAY_KEY_2, "3.3.3.3:443")]


def test_missing_handshake_is_reported_and_recovered_by_reapplying_then_recreating():
    built, parts = manager()
    clock, link = parts["clock"], parts["link"]
    built.start()
    report = built.step()
    assert report["code"] == "HANDSHAKE_MISSING"
    assert report["app_health_ok"] is False and report["db_connect_ok"] is False
    assert [c[0] for c in link.calls] == ["ensure", "peer"]  # grace period after apply

    clock.now += 181
    built.step()
    assert link.calls[-1] == ("peer", RELAY_KEY, "13.124.10.10:51820")  # re-resolve endpoint
    clock.now += 181
    built.step()
    assert ("down",) in link.calls and link.calls[-1][0] == "peer"  # interface recreated

    link.handshake = clock.now
    assert built.step()["code"] is None


def test_stale_handshake_after_a_cut_counts_as_missing():
    built, parts = manager()
    built.start()
    parts["link"].handshake = parts["clock"].now
    assert built.step()["code"] is None
    parts["clock"].now += 200
    report = built.step()
    assert report["code"] == "HANDSHAKE_MISSING"
    assert report["last_handshake_at"] == parts["clock"].now - 200


@pytest.mark.parametrize(
    ("overrides", "code", "app", "db"),
    [
        ({"http_ok": lambda ip, port, path: False}, "APP_HEALTH_FAILED", False, True),
        ({"db_ok": lambda ip, port: False}, "DB_CONNECT_FAILED", True, False),
        (
            {"discover": lambda ports: ({8080: FORWARDS[8080]}, ["FORWARD_TARGET_MISSING"])},
            "FORWARD_TARGET_MISSING",
            True,
            False,
        ),
    ],
)
def test_failed_checks_are_reported_with_a_code(overrides, code, app, db):
    built, parts = manager(**overrides)
    parts["link"].handshake = parts["clock"].now
    built.start()
    report = built.step()
    assert (report["code"], report["app_health_ok"], report["db_connect_ok"]) == (code, app, db)


def test_no_app_port_yet_is_reported():
    client = FakeClient()
    client.config = config(exposed_ports=[5432])
    built, parts = manager(client=client)
    parts["link"].handshake = parts["clock"].now
    built.start()
    report = built.step()
    assert report["code"] == "APP_PORT_NOT_ASSIGNED"
    assert report["app_health_ok"] is False and report["db_connect_ok"] is True


def test_no_relay_reports_a_code_and_keeps_no_tunnel():
    client = FakeClient()
    client.config = config(relays=[])
    built, parts = manager(client=client)
    built.start()
    report = built.step()
    assert report["code"] == "RELAY_NOT_CONFIGURED"
    assert parts["link"].calls == []


def test_interface_failure_is_reported():
    class BrokenLink(FakeLink):
        def ensure(self, plan, private_key):
            raise TunnelAgentError("TUNNEL_INTERFACE_FAILED")

    built, _parts = manager(link=BrokenLink())
    built.start()
    assert built.step()["code"] == "TUNNEL_INTERFACE_FAILED"


def test_platform_outage_keeps_the_running_tunnel():
    built, parts = manager()
    parts["link"].handshake = parts["clock"].now
    built.start()
    built.step()
    parts["client"].fail_config = "PLATFORM_REQUEST_FAILED"
    with pytest.raises(AgentTransportError):
        built.step()
    assert ("down",) not in parts["link"].calls


def test_secrets_never_reach_logs_or_reports():
    events = []
    built, parts = manager(log=events.append)
    parts["link"].handshake = parts["clock"].now
    built.start()
    built.step()
    private = base64.b64encode(parts["keys"].load()).decode()
    text = json.dumps(events) + json.dumps(parts["client"].reports)
    assert private not in text and "cmp_agent_secret" not in text
    assert parts["link"].private_keys == [parts["keys"].load()]


def test_run_loop_retries_platform_outages_and_stops_on_revocation():
    built, parts = manager()
    parts["link"].handshake = parts["clock"].now
    waits, events = [], []
    errors = iter(["PLATFORM_REQUEST_FAILED", None, "AGENT_UNAUTHORIZED"])

    def step():
        code = next(errors)
        if code:
            raise AgentTransportError(code)
        return {"code": None}

    built.step = step
    halted = []

    def wait(seconds):
        waits.append(seconds)
        if seconds == HALT_SECONDS:  # ticket 52 (D14): idle after revocation, no restart loop
            halted.append(("down",) in parts["link"].calls)
            return len(halted) == 3  # SIGTERM after a while
        return False

    exit_code = run_tunnel(built, wait=wait, interval=15, log=events.append)
    assert exit_code == 2
    assert halted == [True, True, True]  # the interface was already down while idling
    assert ("down",) in parts["link"].calls and parts["forwarders"].applied[-1] is None
    assert any(e.get("code") == "PLATFORM_REQUEST_FAILED" for e in events)
    assert [e for e in events if e.get("event") == "stopped"] == [
        {"event": "stopped", "code": "AGENT_UNAUTHORIZED"}
    ]


def test_run_loop_tears_down_on_stop():
    built, parts = manager()
    built.step = lambda: {"code": None}
    assert run_tunnel(built, wait=lambda s: True, interval=15, log=lambda e: None) == 0
    assert ("down",) in parts["link"].calls


# --- key Secret ------------------------------------------------------------------------------


class SecretTarget:
    def __init__(self):
        self.secrets, self.applied = {}, []

    def get(self, kind, name, namespace=None):
        return self.secrets.get((name, namespace))

    def apply(self, document):
        self.applied.append(document)
        meta = document["metadata"]
        self.secrets[(meta["name"], meta["namespace"])] = document


def test_secret_key_store_keeps_the_key_in_the_agent_namespace_only():
    target = SecretTarget()
    store = SecretTunnelKeyStore(target, namespace="cmp-system")
    assert store.load() is None
    key = generate_private_key()
    store.save(key)
    document = target.applied[0]
    assert document["metadata"]["name"] == "cmp-agent-wireguard"
    assert document["metadata"]["namespace"] == "cmp-system"
    assert (
        base64.b64decode(document["data"]["private_key"]).decode() == base64.b64encode(key).decode()
    )
    assert store.load() == key


# --- WireGuard commands (W1) -----------------------------------------------------------------


class Runner:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)
        self.started = []

    def __call__(self, argv, content=None):
        self.calls.append((argv, content))
        key = " ".join(argv)
        code = 1 if any(key.startswith(f) for f in self.fail) else 0
        out = b""
        if key == "wg show wg0 latest-handshakes":
            out = f"{RELAY_KEY}\t1793836800\n".encode()
        if key == "wg show wg0 peers":
            out = f"{RELAY_KEY_2}\n".encode()
        return subprocess.CompletedProcess(argv, code, out, b"")

    def spawn(self, argv, env):
        self.started.append((argv, env))
        self.fail.discard("ip link show dev wg0")
        return type("Proc", (), {"terminate": lambda s: None, "wait": lambda s, timeout: 0})()


def test_kernel_link_passes_the_private_key_on_stdin_and_sets_mtu_and_relay_route():
    runner = Runner(fail={"ip link show dev wg0"})
    link = WireGuardLink(run=runner, spawn=runner.spawn, mode="auto")
    key = generate_private_key()
    plan = plan_from_config(config())
    link.ensure(plan, key)
    link.set_peer(plan)
    argvs = [" ".join(a) for a, _ in runner.calls]
    assert "ip link add dev wg0 type wireguard" in argvs
    assert "ip link set dev wg0 mtu 1420 up" in argvs
    assert "ip address add 10.200.0.2/32 dev wg0" in argvs
    assert "ip route replace 10.200.0.1/32 dev wg0" in argvs
    assert f"wg set wg0 peer {RELAY_KEY_2} remove" in argvs  # stale relay removed
    assert (
        f"wg set wg0 peer {RELAY_KEY} endpoint 13.124.10.10:51820 allowed-ips 10.200.0.1/32 "
        "persistent-keepalive 25"
    ) in argvs
    encoded = base64.b64encode(key).decode()
    assert all(encoded not in a for a in argvs)
    assert (["wg", "set", "wg0", "private-key", "/dev/stdin"], encoded.encode() + b"\n") in (
        runner.calls
    )
    assert link.latest_handshake(RELAY_KEY) == 1793836800
    assert link.latest_handshake(RELAY_KEY_2) is None
    assert runner.started == []


def test_auto_mode_falls_back_to_wireguard_go_when_the_kernel_lacks_wireguard():
    runner = Runner(fail={"ip link show dev wg0", "ip link add"})
    link = WireGuardLink(run=runner, spawn=runner.spawn, mode="auto", wait=lambda s: None)
    link.ensure(plan_from_config(config()), generate_private_key())
    assert runner.started[0][0] == ["wireguard-go", "wg0"]
    assert runner.started[0][1]["WG_PROCESS_FOREGROUND"] == "1"
    assert link.mode_in_use == "userspace"


def test_kernel_mode_does_not_fall_back():
    runner = Runner(fail={"ip link show dev wg0", "ip link add"})
    link = WireGuardLink(run=runner, spawn=runner.spawn, mode="kernel")
    with pytest.raises(TunnelAgentError, match="TUNNEL_INTERFACE_FAILED"):
        link.ensure(plan_from_config(config()), generate_private_key())
    assert runner.started == []


# --- forward target discovery (W2) -----------------------------------------------------------


class ClusterView:
    def __init__(self, namespaces, services, deployments):
        self.namespaces, self.services, self.deployments = namespaces, services, deployments

    def list(self, kind, namespace=None):
        if kind == "namespaces":
            return self.namespaces
        return [s for s in self.services if s["metadata"]["namespace"] == namespace]

    def get(self, kind, name, namespace=None):
        return self.deployments.get((name, namespace))


def ns(name, owned=True):
    annotations = {"cmp.dev/environment-id": "onprem-prod-001"} if owned else {}
    return {"metadata": {"name": name, "annotations": annotations}}


def svc(name, namespace, *ports):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"ports": [{"port": p} for p in ports]},
    }


def deployment(path):
    probe = {"readinessProbe": {"httpGet": {"path": path, "port": "http"}}}
    return {"spec": {"template": {"spec": {"containers": [{"name": "app", **probe}]}}}}


def test_discovery_uses_only_agent_owned_namespaces_and_the_app_health_path():
    view = ClusterView(
        [ns("demo"), ns("kube-system", owned=False), ns("other", owned=False)],
        [
            svc("demo-db", "demo", 5432),
            svc("demo-app", "demo", 8080),
            svc("evil", "other", 8080),
            svc("kube-dns", "kube-system", 53),
        ],
        {("demo-app", "demo"): deployment("/healthz")},
    )
    forwards, codes = discover_forwards(view, [5432, 8080])
    assert codes == []
    assert forwards == {
        5432: Forward(5432, "demo-db.demo.svc", 5432, None),
        8080: Forward(8080, "demo-app.demo.svc", 8080, "/healthz"),
    }


def test_discovery_reports_missing_and_ambiguous_targets():
    view = ClusterView(
        [ns("a"), ns("b")],
        [svc("db-a", "a", 5432), svc("db-b", "b", 5432)],
        {},
    )
    forwards, codes = discover_forwards(view, [5432, 8080])
    assert forwards == {}
    assert codes == ["FORWARD_TARGET_AMBIGUOUS", "FORWARD_TARGET_MISSING"]


def test_app_health_path_defaults_to_health():
    view = ClusterView([ns("demo")], [svc("demo-app", "demo", 8080)], {})
    forwards, _ = discover_forwards(view, [8080])
    assert forwards[8080].health_path == "/health"


# --- real loopback forwarder and probes ------------------------------------------------------


@pytest.fixture
def upstream():
    """Minimal HTTP app answering /health with 200 and anything else with 503."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 503)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_forwarder_relays_allowed_sources_only(upstream):
    port = free_port()
    forwarders = TcpForwarders()
    try:
        forwarders.apply(
            "127.0.0.1", {"127.0.0.1"}, {port: Forward(port, "127.0.0.1", upstream, "/health")}
        )
        assert http_health_ok("127.0.0.1", port, "/health")
        assert not http_health_ok("127.0.0.1", port, "/missing")

        forwarders.apply(
            "127.0.0.1", {"10.200.0.1"}, {port: Forward(port, "127.0.0.1", upstream, "/health")}
        )
        assert not http_health_ok("127.0.0.1", port, "/health")  # source not the relay
    finally:
        forwarders.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_forwarder_is_not_reachable_on_other_addresses(upstream):
    port = free_port()
    forwarders = TcpForwarders()
    try:
        forwarders.apply(
            "127.0.0.1",
            {"127.0.0.1", "127.0.0.2"},
            {port: Forward(port, "127.0.0.1", upstream, "/health")},
        )
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.2", port), timeout=1).close()
    finally:
        forwarders.close()


def test_postgres_probe_expects_an_ssl_request_answer():
    def server(answer):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def serve():
            connection, _ = listener.accept()
            with connection:
                assert connection.recv(8) == bytes.fromhex("0000000804d2162f")
                connection.sendall(answer)
            listener.close()

        threading.Thread(target=serve, daemon=True).start()
        return listener.getsockname()[1]

    assert postgres_ready("127.0.0.1", server(b"N"))
    assert not postgres_ready("127.0.0.1", server(b"HTTP/1.1 400"))
    assert not postgres_ready("127.0.0.1", free_port())


# --- platform client -------------------------------------------------------------------------


def test_client_tunnel_calls_use_the_credential_header_and_methods():
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if request.full_url.endswith("/tunnel/status"):
                return io.BytesIO(b'{"accepted": true}')
            return io.BytesIO(json.dumps(config()).encode())

    client = AgentControlClient("https://platform.example.com")
    client.opener = Opener()
    client.register_tunnel_key(agent_id="agent-001", credential="secret", public_key=RELAY_KEY)
    assert client.tunnel_config(agent_id="agent-001", credential="secret")["mtu"] == 1420
    client.report_tunnel_status(
        agent_id="agent-001", credential="secret", report={"tunnel_ip": "10.200.0.2"}
    )
    assert [r.get_method() for r in requests] == ["PUT", "GET", "POST"]
    assert requests[0].full_url.endswith("/agents/agent-001/tunnel/key")
    assert json.loads(requests[0].data) == {"public_key": RELAY_KEY}
    assert all(r.get_header("Authorization") == "Bearer secret" for r in requests)
    with pytest.raises(AgentTransportError, match="INVALID_AGENT_OR_JOB_ID"):
        client.tunnel_config(agent_id="../x", credential="secret")


# --- ticket 66 (#283): report right after an app port is assigned ------------------------------


class Timeline:
    """Fake clock + `wait` for `run_tunnel`: waiting advances time; stops at `until`."""

    def __init__(self, clock, until):
        self.clock, self.until, self.waits = clock, until, []
        self.events: list[tuple[float, Callable]] = []

    def at(self, when, action):
        self.events.append((when, action))

    def __call__(self, seconds):
        self.waits.append(seconds)
        self.clock.now += seconds
        for when, action in list(self.events):
            if self.clock.now >= when:
                action()
                self.events.remove((when, action))
        return self.clock.now >= self.until


class LiveLink(FakeLink):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def latest_handshake(self, relay_public_key):
        return self.clock.now  # the relay keeps answering


def scenario(until):
    clock = Clock(now=0)
    client = FakeClient()
    client.config = config(exposed_ports=[5432])  # rollout not recorded yet
    built, _ = manager(client=client, clock=clock, link=LiveLink(clock))
    timeline = Timeline(clock, until)
    stamped = []
    original = client.report_tunnel_status

    def report(**kwargs):
        stamped.append((clock.now, kwargs["report"]))
        original(**kwargs)

    client.report_tunnel_status = report
    return built, client, timeline, stamped


def first_healthy(stamped):
    return next(t for t, r in stamped if r["app_health_ok"] and r["code"] is None)


def test_app_port_assigned_after_a_rollout_signal_is_reported_within_seconds():
    built, client, timeline, stamped = scenario(until=900)
    wake = {"token": None}
    # Idle for 10 minutes, then the agent container finishes a rollout (signal) and the
    # platform records READY 30 s later, which assigns the app port.
    timeline.at(600, lambda: wake.update(token="rollout-1"))
    timeline.at(630, lambda: client.config.update(exposed_ports=[5432, 8080]))
    run_tunnel(built, wait=timeline, interval=15, log=lambda e: None, wake=lambda: wake["token"])
    assert 630 <= first_healthy(stamped) <= 633  # not the next 15 s cycle


def test_waiting_for_an_app_port_has_a_call_budget():
    built, _, timeline, stamped = scenario(until=1800)
    run_tunnel(built, wait=timeline, interval=15, log=lambda e: None, wake=lambda: None)
    # startup fast window: at most 120 s / 2 s, then the regular 15 s cycle
    assert len(stamped) <= 120 / 2 + 1800 / 15 + 2
    assert all(r["code"] == "APP_PORT_NOT_ASSIGNED" for _, r in stamped)
    late = [t for t, _ in stamped if t > 130]
    assert all(b - a >= 15 for a, b in pairwise(late))


def test_repeated_signals_never_poll_faster_than_the_fast_interval():
    built, _, timeline, stamped = scenario(until=400)
    counter = {"n": 0}

    def wake():
        counter["n"] += 1  # a new token on every check
        return counter["n"]

    run_tunnel(built, wait=timeline, interval=15, log=lambda e: None, wake=wake)
    times = [t for t, _ in stamped]
    assert all(b - a >= 2 for a, b in pairwise(times))


def test_healthy_tunnel_keeps_the_regular_cycle_even_in_the_fast_window():
    built, client, timeline, stamped = scenario(until=300)
    client.config.update(exposed_ports=[5432, 8080])
    run_tunnel(built, wait=timeline, interval=15, log=lambda e: None, wake=lambda: "same")
    times = [t for t, _ in stamped]
    assert all(r["code"] is None for _, r in stamped)
    assert all(b - a >= 15 for a, b in pairwise(times))


def test_a_port_change_seen_on_a_regular_poll_checks_health_again_quickly():
    built, client, timeline, stamped = scenario(until=900)
    health = {"ok": False}
    built.http_ok = lambda ip, port, path: health["ok"]
    # Port appears without a signal (seen on the regular cycle); the app becomes healthy
    # shortly afterwards and must be reported within the fast interval, not 15 s later.
    timeline.at(600, lambda: client.config.update(exposed_ports=[5432, 8080]))
    timeline.at(620, lambda: health.update(ok=True))
    run_tunnel(built, wait=timeline, interval=15, log=lambda e: None, wake=lambda: None)
    assert 620 <= first_healthy(stamped) <= 623


def test_wake_file_written_by_the_agent_is_read_by_the_tunnel(tmp_path):
    path = tmp_path / "signal" / "rollout"
    assert read_wake(path) is None
    path.parent.mkdir()
    write_wake(path)
    first = read_wake(path)
    write_wake(path)
    assert first is not None and read_wake(path) not in (None, first)


# --- Ticket 73 (#299): K3s API port on the tunnel (ADR-007 step 2) ----------------------------


def test_kube_api_port_forwards_to_the_kubernetes_service_and_is_checked():
    client = FakeClient()
    client.config = config(exposed_ports=[5432, 6443, 8080], kube_api_port=6443)
    seen, probed = [], []
    built, parts = manager(
        client=client,
        expose_kube_api=True,
        discover=lambda ports: (
            seen.append(ports) or ({p: FORWARDS[p] for p in ports if p in FORWARDS}, [])
        ),
        tcp_ok=lambda ip, port: probed.append((ip, port)) or True,
    )
    parts["link"].handshake = parts["clock"].now
    built.start()
    report = built.step()
    assert seen == [[5432, 8080]]  # never looked up among the app Services
    _, sources, forwards = parts["forwarders"].applied[-1]
    assert sources == {"10.200.0.1", "10.200.0.2"}
    assert forwards[6443] == Forward(6443, "kubernetes.default.svc", 443, None)
    assert probed == [("10.200.0.2", 6443)]
    assert report["kube_api_ok"] is True and report["code"] is None


def test_kube_api_failure_is_reported_without_touching_the_app_and_db_checks():
    client = FakeClient()
    client.config = config(exposed_ports=[5432, 6443, 8080], kube_api_port=6443)
    built, parts = manager(client=client, expose_kube_api=True, tcp_ok=lambda ip, port: False)
    parts["link"].handshake = parts["clock"].now
    built.start()
    report = built.step()
    assert report["kube_api_ok"] is False
    assert (report["code"], report["app_health_ok"], report["db_connect_ok"]) == (None, True, True)


def test_without_kube_api_port_the_report_keeps_its_old_shape():
    built, parts = manager()
    parts["link"].handshake = parts["clock"].now
    built.start()
    assert "kube_api_ok" not in built.step()  # older platforms reject unknown fields


@pytest.mark.parametrize("bad", [5432, 0, 70000, "6443x", True])
def test_an_unusable_kube_api_port_is_ignored(bad):
    client = FakeClient()
    client.config = config(kube_api_port=bad)
    built, parts = manager(client=client, expose_kube_api=True)
    parts["link"].handshake = parts["clock"].now
    built.start()
    report = built.step()
    assert "kube_api_ok" not in report
    assert "kubernetes.default.svc" not in str(parts["forwarders"].applied[-1])


def test_agent_forwards_the_kube_api_only_when_its_chart_allows_it():
    client = FakeClient()
    client.config = config(exposed_ports=[5432, 6443, 8080], kube_api_port=6443)
    built, parts = manager(client=client, expose_kube_api=False)
    parts["link"].handshake = parts["clock"].now
    built.start()
    report = built.step()
    assert 6443 not in parts["forwarders"].applied[-1][2]
    assert "kube_api_ok" not in report and report["code"] is None


# --- node CPU/memory on the status report (server-pulse widget, target side) -------------------

METRICS = {"cpu_percent": 12.5, "mem_used_mb": 1024, "mem_total_mb": 4096}


def test_report_carries_node_metrics_when_available_and_omits_them_when_not():
    values = iter([METRICS, None])
    built, parts = manager(metrics=lambda: next(values))
    parts["link"].handshake = parts["clock"].now - 10
    built.start()
    first, second = built.step(), built.step()
    assert parts["client"].reports[0] == first | METRICS
    assert parts["client"].reports[1] == second and "cpu_percent" not in parts["client"].reports[1]


def test_a_rejected_report_is_resent_without_metrics_and_they_stay_off_for_a_while():
    clock = Clock()

    class Strict(FakeClient):  # an older platform: unknown fields are rejected
        def report_tunnel_status(self, *, agent_id, credential, report):
            if "cpu_percent" in report:
                raise AgentTransportError("PLATFORM_REQUEST_FAILED")
            super().report_tunnel_status(agent_id=agent_id, credential=credential, report=report)

    client = Strict()
    built, parts = manager(client=client, clock=clock, metrics=lambda: METRICS)
    parts["link"].handshake = clock.now - 10
    built.start()
    built.step()
    assert len(client.reports) == 1 and "cpu_percent" not in client.reports[0]
    built.step()  # still inside the retry window: no second rejected attempt
    assert len(client.reports) == 2
    clock.now += 301
    built.step()  # window over: tried again (rejected again, resent without)
    assert len(client.reports) == 3 and all("cpu_percent" not in r for r in client.reports)


def test_an_unreadable_metrics_source_never_blocks_the_report():
    def boom():
        raise OSError("no /proc")

    built, parts = manager(metrics=boom)
    parts["link"].handshake = parts["clock"].now - 10
    built.start()
    assert built.step()["code"] is None and len(parts["client"].reports) == 1
