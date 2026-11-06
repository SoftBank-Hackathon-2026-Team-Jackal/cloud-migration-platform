"""Ticket 54 (#189) smoke: can a user Dockerfile RUN step reach the instance metadata service?

The probe build uses the Build module's own `docker buildx build` arguments
(`cmp_worker.image_build._buildx_command`), so the network mode under test is the one the worker
uses. Only HTTP status codes are printed; the fake endpoint serves dummy values.

Local (Docker Desktop VM or a Linux host, needs privileged helper containers):
    uv run python scripts/smoke_build_imds_guard.py --fake-imds

    1. Start a fake IMDSv2 at 169.254.169.254 in its own network namespace, routed through the
       Docker host like the real one (response TTL 2 = EC2 hop limit 2).
    2. Baseline without the guard: the probe RUN reaches it (expected: build fails, REACHABLE).
    3. Apply `infra/demo/build-imds-guard.sh` on the Docker host.
    4. Probe again (expected: build succeeds, BLOCKED); `RUN --network=host` is refused (no
       entitlement); a container on a user-defined bridge (like the compose worker) still reaches it.
    5. Always remove the rule, the fake endpoint, networks and probe images.

Demo host (guard already installed, real IMDS): run without --fake-imds; only step 4 runs.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cmp_worker.image_build import _buildx_command, _docker_env

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "infra/demo/imds-probe/Dockerfile"
GUARD = ROOT / "infra/demo/build-imds-guard.sh"
HELPER = "alpine:3.20"
FAKE_IMAGE = "python:3.11-slim"
FAKE_NAME = "cmp-fake-imds"
USER_NET = "cmp-imds-smoke-net"
PROBE_TAG = "cmp-imds-probe:smoke"
VETH_HOST, VETH_FAKE = "cmpimds-h", "cmpimds-c"
_PLUGIN_FORMAT = '{{range .ClientInfo.Plugins}}{{if eq .Name "buildx"}}{{.Path}}{{end}}{{end}}'
_plugin = subprocess.run(
    ["docker", "info", "--format", _PLUGIN_FORMAT], capture_output=True, text=True, check=False
).stdout.strip()
PLUGIN_DIR = str(Path(_plugin).parent) if _plugin else ""

FAKE_SERVER = r"""
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        ok = self.path == "/latest/api/token"
        self.send_response(200 if ok else 404); self.end_headers()
        self.wfile.write(b"fake-token" if ok else b"")
    def do_GET(self):
        ok = self.headers.get("X-aws-ec2-metadata-token") == "fake-token"
        self.send_response(200 if ok else 401); self.end_headers()
        self.wfile.write(b"fake-role" if ok else b"")
http.server.ThreadingHTTPServer(("0.0.0.0", 80), H).serve_forever()
"""


def sh(*command: str, check: bool = True, stdin: str | None = None, env=None):
    result = subprocess.run(
        command, input=stdin, capture_output=True, text=True, env=env, check=False
    )
    if check and result.returncode:
        raise SystemExit(
            f"command failed ({result.returncode}): {command[:4]}: {result.stderr[-500:]}"
        )
    return result


def on_docker_host(script: str, *, stdin: str | None = None, check: bool = True):
    """Run a shell script in the Docker host's (or Docker Desktop VM's) namespaces."""
    return sh(
        "docker", "run", "--rm", "-i", "--privileged", "--pid=host", "--net=host", HELPER,
        "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "sh", "-c", script,
        check=check, stdin=stdin,
    )  # fmt: skip


def guard(action: str) -> None:
    on_docker_host(f"sh -s {action}", stdin=GUARD.read_text())


def start_fake_imds() -> None:
    sh(
        "docker", "run", "-d", "--name", FAKE_NAME, "--network", "none",
        "--sysctl", "net.ipv4.ip_default_ttl=2", FAKE_IMAGE, "python", "-c", FAKE_SERVER,
    )  # fmt: skip
    pid = sh("docker", "inspect", "-f", "{{.State.Pid}}", FAKE_NAME).stdout.strip()
    # A veth pair puts the endpoint one routed hop beyond the Docker host, like EC2's IMDS, so
    # bridged traffic to it crosses the FORWARD chain (DOCKER-USER) exactly as on the demo host.
    on_docker_host(
        "set -e; "
        f"ip link add {VETH_HOST} type veth peer name {VETH_FAKE}; "
        f"ip link set {VETH_FAKE} netns {pid}; "
        f"ip addr add 169.254.169.1/24 dev {VETH_HOST}; ip link set {VETH_HOST} up; "
        f"nsenter -t {pid} -n ip addr add 169.254.169.254/24 dev {VETH_FAKE}; "
        f"nsenter -t {pid} -n ip link set {VETH_FAKE} up; "
        f"nsenter -t {pid} -n ip route add default via 169.254.169.1"
    )


def probe(platform: str, dockerfile: str) -> tuple[bool, str]:
    """Build the probe with the Build module's arguments; return (succeeded, probe lines)."""
    with tempfile.TemporaryDirectory(prefix="cmp-imds-probe-") as temporary:
        workspace = Path(temporary)
        config = workspace / "docker-config"
        config.mkdir(mode=0o700)
        if PLUGIN_DIR:  # a fresh DOCKER_CONFIG hides per-user CLI plugins (Docker Desktop)
            (config / "config.json").write_text(json.dumps({"cliPluginsExtraDirs": [PLUGIN_DIR]}))
        context = workspace / "context"
        context.mkdir()
        (context / "Dockerfile").write_text(dockerfile)
        command = _buildx_command(
            platform=platform,
            context=context,
            image=PROBE_TAG,
            metadata=workspace / "metadata.json",
        )
        # Smoke-only changes: keep the image locally, always re-run RUN, show RUN output.
        command.remove("--push")
        command[command.index("quiet")] = "plain"
        command.insert(3, "--no-cache")
        result = sh(*command, check=False, env=_docker_env(config))
    lines = [
        line.split("imds_probe", 1)[1].strip()
        for line in (result.stdout + result.stderr).splitlines()
        if "imds_probe " in line and "echo" not in line
    ]
    if "network.host" in result.stderr and "not allowed" in result.stderr:
        lines.append("entitlement network.host not allowed")
    return result.returncode == 0, "; ".join(dict.fromkeys(lines)) or "no probe output"


def user_network_reaches_fake() -> str:
    sh("docker", "network", "create", USER_NET)
    return sh(
        "docker", "run", "--rm", "--network", USER_NET, "curlimages/curl:8.11.1",
        "-s", "-o", "/dev/null", "-w", "%{http_code}", "--connect-timeout", "3", "-X", "PUT",
        "-H", "X-aws-ec2-metadata-token-ttl-seconds: 60",
        "http://169.254.169.254/latest/api/token",
        check=False,
    ).stdout.strip()  # fmt: skip


def cleanup() -> None:
    guard("remove")
    on_docker_host(f"ip link del {VETH_HOST} 2>/dev/null || true", check=False)
    sh("docker", "rm", "-f", FAKE_NAME, check=False)
    sh("docker", "network", "rm", USER_NET, check=False)
    sh("docker", "image", "rm", "-f", PROBE_TAG, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake-imds", action="store_true")
    parser.add_argument("--platform", default=os.environ.get("BUILD_PLATFORM", "linux/amd64"))
    args = parser.parse_args()
    failures = []

    def expect(name: str, ok: bool, detail: str) -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    dockerfile = PROBE.read_text()
    try:
        if args.fake_imds:
            cleanup()
            start_fake_imds()
            built, detail = probe(args.platform, dockerfile)
            expect(
                "baseline without guard: RUN reaches IMDS",
                not built and "token_put=200" in detail and "role_list=000" not in detail,
                detail,
            )
            guard("apply")
        built, detail = probe(args.platform, dockerfile)
        expect(
            "guarded: RUN cannot reach IMDS",
            built and "token_put=000 role_list=000" in detail,
            detail,
        )
        host_network = dockerfile.replace("RUN token_status", "RUN --network=host token_status")
        built, detail = probe(args.platform, host_network)
        expect("RUN --network=host is refused", not built and "not allowed" in detail, detail)
        if args.fake_imds:
            status = user_network_reaches_fake()
            expect("user-defined bridge (worker) still reaches IMDS", status == "200", status)
    finally:
        if args.fake_imds:
            cleanup()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
