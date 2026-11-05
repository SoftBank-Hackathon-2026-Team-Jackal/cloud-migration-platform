"""Opt-in real check for ticket 17 (#71): the in-cluster Agent opens a WireGuard tunnel to a
*test* relay peer and exposes the app and PostgreSQL on its tunnel IP only.

Isolated k3d only (dedicated kubeconfig, `k3d-cmp-*` context). The platform API runs in-process
over HTTPS like ticket 15's smoke. The relay is a plain Docker container on the k3d network with
kernel WireGuard (`10.200.0.1`) whose peers this script syncs from `GET /relays/{id}/agents` —
a test peer standing in for Traffic's relay, not a relay implementation (no Envoy).

Steps: install + approved rollout (as in 15) → tunnel up and reported → health / pg_isready from
the relay over the tunnel → LAN-side blocked → MTU 1420 DF → peer re-registration → relay UDP cut
(iptables) longer than 180 s → relay key + endpoint change → userspace (`wireguard-go`) fallback →
revocation. Secret-free evidence goes to `<state-dir>/evidence.json`.
"""

import argparse
import asyncio
import base64
import contextlib
import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from cmp_api.approvals import claim_execution, complete_execution
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.onprem_rollout import LocalArtifactStore
from cmp_api.tunnels import TunnelStatus
from cmp_providers.onprem_agent import ENVIRONMENT, ClusterTarget
from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.activities.onprem_execution import OnPremExecutionActivities
from onprem_rollout_support import NAMESPACE, approve, build_artifacts, claim_request, plan
from smoke_onprem_agent_install import Platform, make_tls, run, wait_for
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

AGENT_NS = "cmp-system"
AGENT_ID = "agent-tunnel-smoke"
RELEASE = "cmp-agent"
RELAY_ID = "relay-test"
RELAY = "cmp-t17-relay"  # Docker container name of the test peer
APP_PORT = 8080
TUNNEL_IP = "10.200.0.2"


def docker(*argv, check=True, timeout=60):
    return run("docker", *argv, check=check, timeout=timeout)


def relay_sh(script: str, check=True, timeout=30) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", "exec", RELAY, "sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(f"relay command failed: {result.stderr[-300:]}")
    return result


class RelaySync:
    """Test peer only: applies the platform's relay Agent list to the container's wg0."""

    def __init__(self, api: httpx.Client, credential: str):
        self.api, self.credential = api, credential
        self.paused, self.stopped = threading.Event(), threading.Event()
        self.versions: list[str] = []
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stopped.wait(2):
            if self.paused.is_set():
                continue
            with contextlib.suppress(Exception):  # keep polling like a relay would
                self.sync()

    def sync(self):
        response = self.api.get(
            f"/relays/{RELAY_ID}/agents", headers={"Authorization": f"Bearer {self.credential}"}
        )
        response.raise_for_status()
        body = response.json()
        if not self.versions or self.versions[-1] != body["list_version"]:
            self.versions.append(body["list_version"])
        wanted = {a["public_key"]: a for a in body["agents"] if not a["revoked"]}
        current = relay_sh("wg show wg0 peers", timeout=10).stdout.split()
        for peer in current:
            if peer not in wanted:
                relay_sh(f"wg set wg0 peer {peer} remove", timeout=10)
        for peer, agent in wanted.items():
            allowed = ",".join(agent["allowed_ips"])
            relay_sh(f"wg set wg0 peer {peer} allowed-ips {allowed}", timeout=10)
        return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--network", required=True, help="k3d Docker network, e.g. k3d-cmp-t17")
    parser.add_argument("--relay-image", required=True, help="local image with wg/curl/pg_isready")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--agent-image", required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--application-image", required=True)
    parser.add_argument("--platform", choices=["linux/arm64", "linux/amd64"], required=True)
    parser.add_argument("--cut-seconds", type=int, default=200)
    args = parser.parse_args()
    if args.kubeconfig.resolve() == (Path.home() / ".kube/config").resolve():
        raise ValueError("Dedicated kubeconfig required")
    if not args.context.startswith("k3d-cmp-"):
        raise ValueError("Isolated k3d-cmp-* test context required")
    default_config = Path.home() / ".kube/config"
    before = default_config.read_bytes() if default_config.exists() else None
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.state_dir.chmod(0o700)
    kube = ["--kubeconfig", str(args.kubeconfig), "--context", args.context]
    helm_kube = ["--kubeconfig", str(args.kubeconfig), "--kube-context", args.context]

    def kubectl(*a, **kw):
        return run("kubectl", *kube, *a, **kw)

    host = ClusterTarget(args.kubeconfig, args.context, "pending")
    cluster_uid = host.get("namespace", "kube-system")["metadata"]["uid"]
    for namespace in (AGENT_NS, NAMESPACE):
        if host.get("namespace", namespace) is not None:
            raise RuntimeError(f"{namespace} already exists; refusing to reuse it")

    engine = create_engine(f"sqlite+pysqlite:///{args.state_dir / 'business.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)
    store = LocalArtifactStore(args.state_dir / "artifacts")
    ca_crt, cert, key = make_tls(args.state_dir)
    admin_key = secrets.token_urlsafe(24)
    settings.agent_enrollment_admin_key = admin_key
    platform = Platform(db, store, cert, key)
    platform.start()
    api = httpx.Client(
        base_url=f"https://127.0.0.1:{platform.port}",
        verify=False,  # host-side test client only; the Agent verifies the CA
        headers={"X-Enrollment-Admin-Key": admin_key},
    )

    def agent_pod():
        pods = json.loads(
            kubectl(
                "get",
                "pods",
                "-n",
                AGENT_NS,
                "-l",
                "app.kubernetes.io/name=cmp-onprem-agent",
                "-o",
                "json",
            )
        )["items"]
        running = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        return running[0] if running else None

    def logs(container, previous=False):
        pod = agent_pod()
        argv = ["logs", "-n", AGENT_NS, pod["metadata"]["name"], "-c", container]
        if previous:
            argv.append("--previous")
        lines = kubectl(*argv, check=False).splitlines()
        return [json.loads(line) for line in lines if line.startswith("{")]

    def status():
        with db() as session:
            row = session.get(TunnelStatus, AGENT_ID)
            if row is None:
                return None
            return {
                "tunnel_ip": row.tunnel_ip,
                "last_handshake_at": row.last_handshake_at,
                "app_health_ok": row.app_health_ok,
                "db_connect_ok": row.db_connect_ok,
                "code": row.code,
                "reported_at": row.reported_at,
            }

    def healthy_since(moment):
        def check():
            s = status()
            ok = s and s["app_health_ok"] and s["db_connect_ok"] and s["code"] is None
            return (
                ok and s["reported_at"] >= moment and (s["last_handshake_at"] or 0) >= moment and s
            )

        return check

    def over_tunnel():
        http = relay_sh(
            f"curl -sS -m 5 -o /dev/null -w '%{{http_code}}' http://{TUNNEL_IP}:{APP_PORT}/health",
            check=False,
        )
        pg = relay_sh(f"pg_isready -h {TUNNEL_IP} -p 5432 -t 5", check=False)
        return {"app_health_http": http.stdout.strip(), "pg_isready": pg.stdout.strip()}

    async def execute(request, wait):
        activity = OnPremExecutionActivities(db, store, wait_seconds=wait, poll_seconds=1)
        return await ActivityEnvironment().run(activity.execute_on_prem_plan, request)

    def handoff(consumer, plan_id, revision):
        return asyncio.run(
            ActivityEnvironment().run(
                ApprovalActivities(db).get_migration_handoff,
                {"plan_id": plan_id, "revision": revision, "consumer": consumer},
            )
        )

    evidence = {"cluster": {"context": args.context}, "steps": {}}
    steps = evidence["steps"]
    sync = None
    private_values = [admin_key]
    try:
        # 0. Test relay peer: kernel WireGuard 10.200.0.1/24, MTU 1420, UDP 51820.
        docker("rm", "-f", RELAY, check=False)
        docker(
            "run",
            "-d",
            "--name",
            RELAY,
            "--network",
            args.network,
            "--cap-add",
            "NET_ADMIN",
            args.relay_image,
            "sleep",
            "infinity",
        )
        relay_sh(
            "umask 077; wg genkey > /root/wg.key; ip link add wg0 type wireguard && "
            "wg set wg0 listen-port 51820 private-key /root/wg.key && "
            "ip address add 10.200.0.1/24 dev wg0 && ip link set wg0 mtu 1420 up"
        )
        relay_public = relay_sh("wg pubkey < /root/wg.key").stdout.strip()
        relay_ip = docker(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", RELAY
        ).strip()
        registered = api.put(
            f"/tunnel-admin/relays/{RELAY_ID}",
            json={"endpoint": f"{relay_ip}:51820", "public_key": relay_public},
        )
        registered.raise_for_status()
        relay_credential = registered.json()["credential"]
        private_values.append(relay_credential)
        sync = RelaySync(api, relay_credential)
        sync.thread.start()
        steps["relay_peer"] = {
            "endpoint_port": 51820,
            "config_version": registered.json()["config_version"],
        }

        # 1. Install the Agent (tunnel container on) and run an approved rollout as in 15.
        issued = api.post(
            "/agent-enrollments", json={"owner_id": "owner-smoke", "agent_id": AGENT_ID}
        )
        issued.raise_for_status()
        token = issued.json()["enrollment_token"]
        private_values.append(token)
        repository, tag = args.agent_image.rsplit(":", 1)
        values = {
            "agentId": AGENT_ID,
            "platform": {
                "url": f"https://host.k3d.internal:{platform.port}",
                "caBundle": ca_crt.read_text(),
            },
            "enrollmentToken": token,
            "image": {"repository": repository, "tag": tag, "digest": "", "pullPolicy": "Never"},
            "idleSeconds": 2,
            "helmTimeout": "120s",
            "tunnel": {"intervalSeconds": 5},
        }
        values_file = args.state_dir / "agent-values.json"
        values_file.write_text(json.dumps(values))
        values_file.chmod(0o600)
        run(
            "helm",
            *helm_kube,
            "install",
            RELEASE,
            str(ROOT / "infra/helm/onprem-agent"),
            "-n",
            AGENT_NS,
            "--create-namespace",
            "-f",
            str(values_file),
            "--wait",
            "--timeout",
            "180s",
            timeout=240,
        )
        values_file.unlink()
        kubectl(
            "apply",
            "-f",
            "-",
            content=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": NAMESPACE,
                        "annotations": {ENVIRONMENT: "onprem-prod-001"},
                    },
                }
            ),
        )
        kubectl(
            "create",
            "secret",
            "generic",
            "demo-app-env",
            "-n",
            NAMESPACE,
            "--from-literal=API_KEY=" + secrets.token_urlsafe(16),
        )
        images = {
            "db_image": args.database_image,
            "app_image": args.application_image,
            "platforms": (args.platform,),
        }
        artifacts = build_artifacts(store, args.state_dir, **images)
        with db() as session:
            manifest = plan(artifacts, agent_id=AGENT_ID, cluster_uid=cluster_uid)
            record = approve(session, manifest)
            request = claim_request(record, manifest)
            approval_id = record.id
        before_tunnel_status = status()
        activity = asyncio.run(execute(request, 600))
        # Store the READY ON_PREM result (13) so the platform exposes the app port (16 T5).
        with db() as session:
            claim_execution(session, approval_id)
            complete_execution(
                session,
                approval_id,
                status="SUCCEEDED",
                code="READY",
                step="execute_on_prem_plan",
                resource_refs=activity["resource_refs"],
            )
        plan_id, revision = request["plan_id"], request["revision"]
        steps["rollout_ready"] = {"activity_code": activity["code"], "status": activity["status"]}

        # 2. Tunnel up and reported: handshake + app health + DB through the tunnel address.
        started = int(time.time())
        reported = wait_for(healthy_since(started - 600), timeout=240, what="tunnel healthy")
        tunnel_events = logs("tunnel")
        traffic = handoff("TRAFFIC_MIGRATION", plan_id, revision)
        data = handoff("DATA_MIGRATION", plan_id, revision)
        steps["tunnel_up"] = {
            "status_before_rollout": before_tunnel_status,
            "reported": reported,
            "tunnel_log_events": [
                {
                    k: e.get(k)
                    for k in ("event", "code", "mode", "config_version", "action")
                    if k in e
                }
                for e in tunnel_events
            ],
            "relay_wg_peer_handshakes": relay_sh("wg show wg0 latest-handshakes").stdout.count(
                "\t"
            ),
            "relay_list_versions": list(sync.versions),
            "traffic_handoff": {
                "proceed": traffic["proceed"],
                "blocked_by": traffic["blocked_by"],
                "external_host": traffic["application"]["external_host"],
                "external_port": traffic["application"]["external_port"],
                "external_endpoint": traffic["application"]["external_endpoint"],
                "health_path": traffic["application"]["health_path"],
            },
            "data_handoff": {
                "proceed": data["proceed"],
                "tunnel_endpoint": data["database"]["tunnel_endpoint"],
            },
        }

        # 3. From the relay side over the tunnel.
        steps["relay_side_over_tunnel"] = over_tunnel()

        # 4. Not reachable except via the tunnel IP.
        pod = agent_pod()
        pod_ip = pod["status"]["podIP"]
        node_ips = docker(
            "network",
            "inspect",
            args.network,
            "-f",
            "{{range .Containers}}{{.Name}}={{.IPv4Address}} {{end}}",
        ).split()
        nodes = [
            entry.split("=")[1].split("/")[0]
            for entry in node_ips
            if "-server-" in entry or "-agent-" in entry
        ]
        lan = {}
        for address in nodes:
            lan[f"node:{APP_PORT}"] = (
                relay_sh(
                    f"curl -sS -m 3 -o /dev/null -w '%{{http_code}}' http://{address}:{APP_PORT}/health",
                    check=False,
                ).returncode
                != 0
            )
            lan["node:5432"] = (
                relay_sh(f"pg_isready -h {address} -p 5432 -t 3", check=False).returncode != 0
            )
        # A LAN host that even has a route to the Pod network gets nothing on the Pod IP.
        relay_sh(f"ip route replace 10.42.0.0/16 via {nodes[0]}")
        lan[f"agent_pod_ip:{APP_PORT}"] = (
            relay_sh(
                f"curl -sS -m 3 -o /dev/null http://{pod_ip}:{APP_PORT}/health", check=False
            ).returncode
            != 0
        )
        lan["agent_pod_ip:5432"] = (
            relay_sh(f"pg_isready -h {pod_ip} -p 5432 -t 3", check=False).returncode != 0
        )
        lan["pod_network_route_works"] = (
            relay_sh(f"ping -c 1 -W 2 {pod_ip}", check=False).returncode == 0
        )  # the route is real, so the refusals above are not routing failures
        relay_sh("ip route del 10.42.0.0/16", check=False)
        listeners = kubectl(
            "exec",
            "-n",
            AGENT_NS,
            pod["metadata"]["name"],
            "-c",
            "tunnel",
            "--",
            "ss",
            "-ltnH",
        ).split("\n")
        steps["lan_blocked"] = {
            "blocked": lan,
            "tunnel_container_listeners": sorted(
                line.split()[3] for line in listeners if line.strip()
            ),
            "nodes_checked": len(nodes),
        }

        # 5. MTU 1420 with DF: 1392 + 28 = 1420 passes, 1393 does not fit.
        fits = relay_sh(f"ping -c 3 -W 2 -M do -s 1392 {TUNNEL_IP}", check=False)
        too_big = relay_sh(f"ping -c 1 -W 2 -M do -s 1393 {TUNNEL_IP}", check=False)
        wg_mtu = kubectl(
            "exec",
            "-n",
            AGENT_NS,
            pod["metadata"]["name"],
            "-c",
            "tunnel",
            "--",
            "ip",
            "-o",
            "link",
            "show",
            "dev",
            "wg0",
        )
        steps["mtu_df"] = {
            "agent_wg0_mtu_1420": " mtu 1420 " in wg_mtu,
            "df_1420_ok": fits.returncode == 0,
            "df_1420_summary": [line for line in fits.stdout.splitlines() if "packet loss" in line],
            "df_1421_rejected": too_big.returncode != 0,
        }

        # 6. Peer re-registration on the relay (remove; the sync re-adds it).
        agent_peer = next(
            line.split("\t")[0]
            for line in relay_sh("wg show wg0 allowed-ips").stdout.splitlines()
            if TUNNEL_IP in line
        )
        cut = int(time.time())
        relay_sh(f"wg set wg0 peer {agent_peer} remove")
        recovered = wait_for(
            lambda: over_tunnel()["app_health_http"] == "200",
            timeout=120,
            interval=0.5,
            what="peer re-registration recovery",
        )
        steps["peer_reregistration"] = {
            "recovered": bool(recovered),
            "seconds": int(time.time()) - cut,
        }

        # 7. Relay cut longer than the 180 s handshake limit, then restored.
        # Drop all WireGuard UDP on the relay (`docker pause` would not cut it: kernel WireGuard
        # keeps answering while the container's processes are frozen).
        drop = "iptables -{op} INPUT -p udp --dport 51820 -j DROP; iptables -{op} OUTPUT -p udp --sport 51820 -j DROP"
        relay_sh(drop.format(op="A"))
        cut = int(time.time())
        time.sleep(args.cut_seconds)
        during = status()
        traffic_during = handoff("TRAFFIC_MIGRATION", plan_id, revision)
        recover_events = [e for e in logs("tunnel") if e["event"] == "tunnel_recover"]
        relay_sh(drop.format(op="D"))
        restored = int(time.time())
        after = wait_for(
            healthy_since(restored), timeout=240, interval=1, what="recovery after cut"
        )
        steps["relay_cut"] = {
            "cut_seconds": restored - cut,
            "status_during_cut": during,
            "traffic_handoff_during_cut": {
                "proceed": traffic_during["proceed"],
                "blocked_by": traffic_during["blocked_by"],
            },
            "recover_actions_during_cut": [e.get("action") for e in recover_events],
            "status_after": after,
            "recovered_after_seconds": after["reported_at"] - restored,
            "relay_side_over_tunnel": over_tunnel(),
        }

        # 8. Relay key and endpoint change (config_version bump) → the Agent re-applies.
        relay_sh(
            "umask 077; wg genkey > /root/wg2.key && "
            "wg set wg0 private-key /root/wg2.key listen-port 51821"
        )
        new_public = relay_sh("wg pubkey < /root/wg2.key").stdout.strip()
        changed = int(time.time())
        updated = api.put(
            f"/tunnel-admin/relays/{RELAY_ID}",
            json={"endpoint": f"{relay_ip}:51821", "public_key": new_public},
        )
        updated.raise_for_status()
        after_change = wait_for(
            healthy_since(changed), timeout=180, interval=1, what="rekey recovery"
        )
        applied = [e for e in logs("tunnel") if e["event"] == "tunnel_applied"]
        steps["relay_key_endpoint_change"] = {
            "config_version": updated.json()["config_version"],
            "agent_applied_versions": [e["config_version"] for e in applied],
            "recovered_after_seconds": after_change["reported_at"] - changed,
            "relay_side_over_tunnel": over_tunnel(),
            "relay_public_key_changed": new_public != relay_public,
        }

        # 9. Private key only in the Agent-namespace Secret.
        secret = json.loads(
            kubectl("get", "secret", "cmp-agent-wireguard", "-n", AGENT_NS, "-o", "json")
        )
        private_b64 = base64.b64decode(secret["data"]["private_key"]).decode().strip()
        private_values.append(private_b64)
        agent_logs = kubectl(
            "logs", "-n", AGENT_NS, agent_pod()["metadata"]["name"], "--all-containers"
        )
        platform_db = (args.state_dir / "business.db").read_bytes()
        relay_list = json.dumps(sync.sync())
        steps["private_key"] = {
            "secret_labels": secret["metadata"].get("labels"),
            "helm_managed": "meta.helm.sh/release-name"
            in (secret["metadata"].get("annotations") or {}),
            "absent_from_agent_logs": private_b64 not in agent_logs,
            "absent_from_platform_db": private_b64.encode() not in platform_db,
            "absent_from_relay_list": private_b64 not in relay_list,
        }

        # 10. Userspace fallback (wireguard-go) via the Chart value.
        old = agent_pod()["metadata"]["name"]
        run(
            "helm",
            *helm_kube,
            "upgrade",
            RELEASE,
            str(ROOT / "infra/helm/onprem-agent"),
            "-n",
            AGENT_NS,
            "--reuse-values",
            "--set",
            "enrollmentToken=",
            "--set",
            "tunnel.mode=userspace",
            "--wait",
            "--timeout",
            "180s",
            timeout=240,
        )
        wait_for(
            lambda: (
                (p := agent_pod())
                and p["metadata"]["name"] != old
                and p["status"].get("phase") == "Running"
            ),
            what="userspace pod",
        )
        switched = int(time.time())
        user_ok = wait_for(
            healthy_since(switched), timeout=240, interval=1, what="userspace tunnel"
        )
        pod = agent_pod()
        link = kubectl(
            "exec",
            "-n",
            AGENT_NS,
            pod["metadata"]["name"],
            "-c",
            "tunnel",
            "--",
            "ip",
            "-d",
            "-o",
            "link",
            "show",
            "dev",
            "wg0",
        )
        steps["userspace_fallback"] = {
            "applied_modes": [
                e.get("mode") for e in logs("tunnel") if e["event"] == "tunnel_applied"
            ],
            "link_is_tun": " tun " in link,
            "status": user_ok,
            "relay_side_over_tunnel": over_tunnel(),
            "same_private_key": base64.b64decode(
                json.loads(
                    kubectl("get", "secret", "cmp-agent-wireguard", "-n", AGENT_NS, "-o", "json")
                )["data"]["private_key"]
            )
            .decode()
            .strip()
            == private_b64,
        }

        # 11. Revocation: the tunnel container stops (exit 2) and the relay drops the peer.
        api.post(f"/agent-admin/agents/{AGENT_ID}/revoke").raise_for_status()

        def tunnel_terminated():
            statuses = agent_pod()["status"]["containerStatuses"]
            state = next(s for s in statuses if s["name"] == "tunnel")
            return state.get("lastState", {}).get("terminated") or state.get("state", {}).get(
                "terminated"
            )

        terminated = wait_for(tunnel_terminated, timeout=120, what="tunnel exit after revocation")
        time.sleep(5)
        steps["revoked"] = {
            "tunnel_exit_code": terminated.get("exitCode"),
            "relay_peers_left": len(relay_sh("wg show wg0 peers").stdout.split()),
            "relay_side_over_tunnel": over_tunnel(),
        }
    except Exception as exc:
        # Codes only: the last platform-side status and the tunnel container's event codes.
        diagnostics = {"error": type(exc).__name__, "what": str(exc)[:120], "status": status()}
        try:
            diagnostics["tunnel_events"] = [
                {k: e.get(k) for k in ("event", "code", "mode", "action") if k in e}
                for e in logs("tunnel")[-15:]
            ]
            diagnostics["pods"] = kubectl("get", "pods", "-A", "--no-headers", check=False)[-1500:]
        except Exception as inner:  # noqa: BLE001 - diagnostics are best effort
            diagnostics["diagnostics_error"] = type(inner).__name__
        evidence["failure"] = diagnostics
        raise
    finally:
        cleanup = {}
        if sync:
            sync.stopped.set()
        cleanup["relay_container_removed"] = (
            subprocess.run(
                ["docker", "rm", "-f", RELAY], capture_output=True, check=False
            ).returncode
            == 0
        )
        cleanup["agent_release_uninstalled"] = (
            subprocess.run(
                ["helm", *helm_kube, "uninstall", RELEASE, "-n", AGENT_NS, "--wait"],
                check=False,
                capture_output=True,
                timeout=180,
            ).returncode
            == 0
        )
        for namespace in (NAMESPACE, AGENT_NS):
            cleanup[f"namespace_{namespace}_deleted"] = (
                subprocess.run(
                    [
                        "kubectl",
                        *kube,
                        "delete",
                        "namespace",
                        namespace,
                        "--ignore-not-found",
                        "--wait=true",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=240,
                ).returncode
                == 0
            )
        evidence["cleanup"] = cleanup
        after_config = default_config.read_bytes() if default_config.exists() else None
        evidence["default_kubeconfig_unchanged"] = before == after_config
        text = json.dumps(evidence, indent=2)
        leaked = [i for i, value in enumerate(private_values) if value and value in text]
        evidence["secrets_absent_from_evidence"] = not leaked
        text = json.dumps(evidence, indent=2)
        (args.state_dir / "evidence.json").write_text(text)
        print(text)
        if platform.server:
            platform.server.should_exit = True
        engine.dispose()


if __name__ == "__main__":
    main()
