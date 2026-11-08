import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cmp_worker.egress_proxy import Allowlist, make_handler


class Target(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = b'{"hello":"world"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def world():
    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    records = []
    allow = Allowlist([{"host": "127.0.0.1", "port": target.server_port}])
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(allow, records.append))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    yield target.server_port, proxy.server_port, records
    target.shutdown()
    proxy.shutdown()


def test_allowlist_matches_exact_hosts_and_wildcard_suffixes_per_port():
    allow = Allowlist(
        [{"host": "api.pay.example.com", "port": 443}, {"host": "*.amazonaws.com", "port": 443}]
    )
    assert allow.permits("API.pay.example.com", 443)
    assert allow.permits("s3.ap-northeast-2.amazonaws.com", 443)
    assert not allow.permits("api.pay.example.com", 80)
    assert not allow.permits("evil-amazonaws.com", 443) and not allow.permits("amazonaws.com", 443)
    assert not allow.permits("other.example.com", 443)


def test_a_listed_http_request_is_forwarded_and_logged_without_content(world):
    target_port, proxy_port, records = world
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port)
    conn.request(
        "GET",
        f"http://127.0.0.1:{target_port}/x?token=abc",
        headers={"Authorization": "Bearer s3cr3t"},
    )
    response = conn.getresponse()
    assert response.status == 200 and json.loads(response.read()) == {"hello": "world"}
    assert records == [
        {"decision": "ALLOW", "host": "127.0.0.1", "port": target_port, "method": "GET"}
    ]
    assert "s3cr3t" not in json.dumps(records) and "token" not in json.dumps(records)


def test_an_unlisted_host_is_refused_and_recorded(world):
    _, proxy_port, records = world
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port)
    conn.request("GET", "http://origin-db.internal.example.com:5432/")
    assert conn.getresponse().status == 403
    assert records == [
        {
            "decision": "BLOCK",
            "host": "origin-db.internal.example.com",
            "port": 5432,
            "method": "GET",
        }
    ]


def test_connect_is_checked_the_same_way(world):
    target_port, proxy_port, records = world
    refused = http.client.HTTPConnection("127.0.0.1", proxy_port)
    refused.set_tunnel("blocked.example.com", 443)
    with pytest.raises(OSError):
        refused.request("GET", "/")
    allowed = http.client.HTTPConnection("127.0.0.1", proxy_port)
    allowed.set_tunnel("127.0.0.1", target_port)
    allowed.request("GET", "/")
    assert allowed.getresponse().status == 200
    assert [(r["decision"], r["host"]) for r in records] == [
        ("BLOCK", "blocked.example.com"),
        ("ALLOW", "127.0.0.1"),
    ]


# --- the platform's own OTLP ingest, allowed by address range (same value as production) -------

TELEMETRY = {"cidr": "13.209.240.49/32", "port": 443}


def test_a_name_that_resolves_into_the_telemetry_range_gets_that_address():
    resolved = {"telemetry.example.com": ["13.209.240.49"], "other.example.com": ["8.8.8.8"]}
    allow = Allowlist([], cidrs=[TELEMETRY], resolve=lambda host: resolved.get(host, []))
    assert allow.address_for("telemetry.example.com", 443) == "13.209.240.49"
    assert allow.address_for("other.example.com", 443) is None  # resolves outside the range
    assert allow.address_for("unknown.example.com", 443) is None  # does not resolve
    assert allow.address_for("telemetry.example.com", 80) is None  # the port is part of the rule


def test_mixed_answers_use_only_the_address_inside_the_range():
    allow = Allowlist([], cidrs=[TELEMETRY], resolve=lambda host: ["8.8.8.8", "13.209.240.49"])
    assert allow.address_for("mixed.example.com", 443) == "13.209.240.49"


def test_an_address_literal_needs_no_lookup_and_no_range_means_no_exception():
    def no_lookup(host):
        raise AssertionError("a literal address must not be looked up")

    allow = Allowlist([], cidrs=[TELEMETRY], resolve=no_lookup)
    assert allow.address_for("13.209.240.49", 443) == "13.209.240.49"
    assert allow.address_for("13.209.240.50", 443) is None
    assert Allowlist([]).address_for("13.209.240.49", 443) is None


def test_connect_to_a_name_inside_the_range_is_allowed_and_dials_the_matched_address():
    seen = []

    def serve(listener):
        conn, _ = listener.accept()
        seen.append(conn.recv(16))
        conn.sendall(b"pong")
        conn.close()

    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    threading.Thread(target=serve, args=(listener,), daemon=True).start()
    records = []
    allow = Allowlist(
        [],
        cidrs=[{"cidr": "127.0.0.0/8", "port": port}],
        resolve=lambda host: ["127.0.0.1"] if host == "telemetry.invalid" else [],
    )
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(allow, records.append))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        ok = http.client.HTTPConnection("127.0.0.1", proxy.server_port)
        ok.set_tunnel("telemetry.invalid", port)
        ok.connect()
        ok.sock.sendall(b"ping")
        assert ok.sock.recv(16) == b"pong"
        refused = http.client.HTTPConnection("127.0.0.1", proxy.server_port)
        refused.set_tunnel("elsewhere.invalid", port)
        with pytest.raises(OSError):
            refused.request("GET", "/")
    finally:
        proxy.shutdown()
        listener.close()
    assert seen == [b"ping"]
    # The decision log still names the host the app asked for, never the address behind it.
    assert [(r["decision"], r["host"], r["port"]) for r in records] == [
        ("ALLOW", "telemetry.invalid", port),
        ("BLOCK", "elsewhere.invalid", port),
    ]
