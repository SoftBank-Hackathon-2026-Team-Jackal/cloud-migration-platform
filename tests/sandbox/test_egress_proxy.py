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
