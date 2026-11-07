"""Provisioning 81 (#321): the gateway start script recovers from a restart and a late NLB DNS name.

2026-11-07 (migration-cf7c5262-…): the k3d gateway started 5 s after the source NLB was created and
exited; every restart then found the old `wg0` in the same Pod network, `ip link add` failed and the
`wireguard-go` fallback died with "Failed to create TUN device", so `dm_tunnel` waited for a
handshake that never came.

Seam: `wg.START_SCRIPT` run by `/bin/sh` with fake `apk`/`ip`/`wg`/`wireguard-go`/`socat`/`mknod`/
`sleep` on PATH. The fakes keep `wg0` as a file and log their arguments.
Misses: a real kernel, real WireGuard and DNS (checked on the demo k3d).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from cmp_data import tunnel as wg

FAKES = {
    "apk": "exit 0",
    "socat": "exit 0",
    "mknod": "exit 0",
    "sleep": "exit 0",
    # Like the demo k3d: a TUN device named after a leftover wg0 fails ("invalid argument").
    "wireguard-go": 'echo "wireguard-go $*" >> "$S/log"; [ -e "$S/wg0" ] && exit 1; exit 0',
    "ip": """echo "ip $*" >> "$S/log"
case "$*" in
  "link del wg0") [ -e "$S/wg0" ] && rm -f "$S/wg0" "$S/addr" && exit 0; exit 1 ;;
  "link add wg0 type wireguard")
    [ -n "${NO_KMOD:-}" ] && exit 2; [ -e "$S/wg0" ] && exit 2; touch "$S/wg0" ;;
  "addr add"*) [ -e "$S/addr" ] && exit 2; touch "$S/addr" ;;
esac
exit 0""",
    "wg": """echo "wg $*" >> "$S/log"
case "$*" in *endpoint*)
  n=$(cat "$S/dns" 2>/dev/null || echo 0)
  if [ "$n" -lt "${DNS_FAILS:-0}" ]; then echo $((n + 1)) > "$S/dns"
    echo "Name does not resolve" >&2; exit 1; fi ;;
esac
exit 0""",
}


def run(tmp_path: Path, *, endpoint: str = "nlb.example.com:51820", **env: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in FAKES.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    ready = tmp_path / "started"
    script = wg.START_SCRIPT.replace("/tmp/started", str(ready)).replace(
        "/dev/net", str(tmp_path / "dev-net")
    )
    proc = subprocess.run(
        ["/bin/sh", "-c", script],
        check=False,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "S": str(tmp_path),
            "WG_IP": "10.210.0.2",
            "PEER_IP": "10.210.0.1",
            "PEER_PUBLIC_KEY": "PUBLIC",
            "PEER_ENDPOINT": endpoint,
            "LOCAL_DB": "db.ns.svc",
            "POD_IP": "10.42.0.9",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    log = (tmp_path / "log").read_text().splitlines()
    return proc, log, ready.exists()


def test_a_fresh_start_uses_kernel_wireguard_and_sets_the_endpoint(tmp_path) -> None:
    proc, log, ready = run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert ready
    assert "ip link add wg0 type wireguard" in log
    assert not any(line.startswith("wireguard-go") for line in log)
    assert "wg set wg0 peer PUBLIC endpoint nlb.example.com:51820" in log


def test_a_restart_removes_the_old_wg0_first(tmp_path) -> None:
    (tmp_path / "wg0").touch()  # left in the Pod network by the previous container
    (tmp_path / "addr").touch()

    proc, log, ready = run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert ready
    assert log.index("ip link del wg0") < log.index("ip link add wg0 type wireguard")
    assert not any(line.startswith("wireguard-go") for line in log)


def test_an_unresolved_endpoint_is_retried_until_it_resolves(tmp_path) -> None:
    proc, log, ready = run(tmp_path, DNS_FAILS="3")

    assert proc.returncode == 0, proc.stderr
    assert ready  # the Pod is ready; dm_tunnel's handshake wait is the real limit
    assert sum("endpoint nlb.example.com:51820" in line for line in log) == 4
    assert proc.stderr.count("PEER_ENDPOINT_UNRESOLVED nlb.example.com:51820") == 3


def test_the_ingress_side_has_no_endpoint_to_resolve(tmp_path) -> None:
    proc, log, ready = run(tmp_path, endpoint="")

    assert proc.returncode == 0, proc.stderr
    assert ready
    assert not any("endpoint" in line for line in log)


def test_without_kernel_wireguard_the_userspace_fallback_still_runs(tmp_path) -> None:
    proc, log, _ready = run(tmp_path, NO_KMOD="1")

    assert proc.returncode == 0, proc.stderr
    assert "wireguard-go wg0" in log


# ---- Provisioning 83: 원본 고정 게이트웨이(STANDING_SCRIPT) ----

STANDING_WG = """echo "wg $*" >> "$S/log"
[ "$*" = "show wg0 peers" ] && cat "$S/peers" 2>/dev/null
exit 0"""


def run_standing(tmp_path: Path, peer: str | None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in {**FAKES, "wg": STANDING_WG}.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    peer_file = tmp_path / "run" / "peer"
    if peer is not None:
        peer_file.parent.mkdir(exist_ok=True)
        peer_file.write_text(peer)
    script = wg.STANDING_SCRIPT.replace("/tmp/started", str(tmp_path / "started")).replace(
        "/dev/net", str(tmp_path / "dev-net")
    )
    proc = subprocess.run(
        ["/bin/sh", "-c", script], check=False, capture_output=True, text=True, timeout=20,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "S": str(tmp_path),
             "WG_IP": "10.210.0.1", "PEER_IP": "10.210.0.2", "KEEPALIVE": "25",
             "HEALTH_PORT": "8080", "POD_IP": "10.20.0.9", "PEER_FILE": str(peer_file),
             "STANDING_ONCE": "1"},
    )  # fmt: skip
    log = (tmp_path / "log").read_text().splitlines()
    return proc, log


def test_standing_gateway_starts_without_a_peer(tmp_path) -> None:
    proc, log = run_standing(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "started").exists()
    assert "wg set wg0 private-key /etc/wg/private listen-port 51820" in log
    assert not any("allowed-ips" in line for line in log)
    assert "STANDING_PEER none" in proc.stdout


def test_standing_gateway_swaps_the_old_peer_for_the_new_one(tmp_path) -> None:
    (tmp_path / "peers").write_text("OLDKEY\n")
    proc, log = run_standing(tmp_path, "cmp-wg-1234 NEWKEY db.ns.svc\n")
    assert proc.returncode == 0, proc.stderr
    removed = log.index("wg set wg0 peer OLDKEY remove")
    added = log.index("wg set wg0 peer NEWKEY allowed-ips 10.210.0.2/32 persistent-keepalive 25")
    assert removed < added
    assert "STANDING_PEER cmp-wg-1234" in proc.stdout
