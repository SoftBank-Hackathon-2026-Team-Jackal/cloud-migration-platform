"""#225: the demo edge routes measured with the real Caddy image and `agent-routes.caddy`.

Fake api and web upstreams run in this process; Caddy runs in Docker (the image pinned in
`infra/demo/compose.yaml`, plain HTTP instead of the ACME site block) and reaches them through
`host.docker.internal`. Skipped when Docker or that image is not available locally (no pull).
"""

import http.client
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

DEMO = Path(__file__).resolve().parents[1] / "infra" / "demo"
CADDY_IMAGE = yaml.safe_load((DEMO / "compose.yaml").read_text())["services"]["edge"]["image"]
SSE_GAP = 3.0


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", CADDY_IMAGE], capture_output=True, check=False
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(), reason=f"needs Docker and {CADDY_IMAGE}")


def _upstream(name: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _reply(self):
            if name == "api" and self.path.endswith("/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"id: 1\ndata: first\n\n")
                self.wfile.flush()
                time.sleep(SSE_GAP)
                self.wfile.write(b"id: 2\ndata: second\n\n")
                self.wfile.flush()
                self.close_connection = True
                return
            body = f"{name} {self.command} {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _reply

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(scope="module")
def edge(tmp_path_factory):
    api, web = _upstream("api"), _upstream("web")
    site = tmp_path_factory.mktemp("edge") / "Caddyfile"
    site.write_text(
        "{\n\tauto_https off\n\tadmin off\n}\n:8080 {\n\timport agent-routes.caddy\n}\n"
    )
    name = f"cmp-edge-test-{uuid.uuid4().hex[:8]}"
    env = {
        "CMP_API_UPSTREAM": f"host.docker.internal:{api.server_address[1]}",
        "CMP_WEB_UPSTREAM": f"host.docker.internal:{web.server_address[1]}",
        "CMP_TEMPORAL_UPSTREAM": "127.0.0.1:1",
        "CMP_TEMPORAL_ALLOWED_IP": "192.0.2.1/32",
    }
    command = ["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::8080"]
    command += ["--add-host", "host.docker.internal:host-gateway"]
    for key, value in env.items():
        command += ["-e", f"{key}={value}"]
    command += ["-v", f"{site}:/etc/caddy/Caddyfile:ro"]
    command += ["-v", f"{DEMO / 'agent-routes.caddy'}:/etc/caddy/agent-routes.caddy:ro"]
    command += [CADDY_IMAGE]
    subprocess.run(command, check=True, capture_output=True)
    try:
        mapped = subprocess.run(
            ["docker", "port", name, "8080/tcp"], check=True, capture_output=True, text=True
        ).stdout.split()[0]
        port = int(mapped.rsplit(":", 1)[1])
        for _ in range(50):
            try:
                _get(port, "/")
                break
            except OSError:
                time.sleep(0.2)
        yield port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        api.shutdown()
        web.shutdown()


def _get(port: int, path: str, method: str = "GET") -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/migrations"),
        ("GET", "/migrations/x"),
        ("GET", "/migrations/x/report"),
        ("POST", "/provisioning/plans/p/revisions/1/approvals"),
        ("POST", "/agents/register"),
        ("GET", "/relays/r1/agents"),
    ],
)
def test_backend_paths_reach_the_api(edge, method, path):
    assert _get(edge, path, method) == (200, f"api {method} {path}")


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/settings",
        "/api/auth/session",
        "/api/agents",
        "/migrationsX",
        "/provisioning",
        "/provisioning-admin/teardowns",
        "/agent-admin/agents",
        "/agent-enrollments",
        "/tunnel-admin/x",
        "/connections",
        "/env-files",
        "/docs",
        "/openapi.json",
    ],
)
def test_other_paths_reach_the_web(edge, path):
    assert _get(edge, path) == (200, f"web GET {path}")


def test_dot_segments_cannot_reach_operator_routes_through_a_public_prefix(edge):
    # Caddy cleans the path before matching: /migrations/../agent-admin/... is /agent-admin/...
    status, body = _get(edge, "/migrations/../agent-admin/agents")
    assert status == 200 and body.startswith("web ")


def test_migration_events_stream_through_unbuffered(edge):
    connection = http.client.HTTPConnection("127.0.0.1", edge, timeout=10)
    try:
        started = time.monotonic()
        connection.request("GET", "/migrations/x/events", headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream"
        first = b""
        while not first.endswith(b"\n\n"):
            first += response.read1(64)
        # The first event arrives well before the upstream sends the second one.
        assert time.monotonic() - started < SSE_GAP / 2
        assert first == b"id: 1\ndata: first\n\n"
        rest = response.read()
        assert rest == b"id: 2\ndata: second\n\n"
    finally:
        connection.close()
