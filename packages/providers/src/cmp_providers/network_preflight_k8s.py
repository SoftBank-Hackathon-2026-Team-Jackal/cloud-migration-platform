"""On-site Network Preflight executor on Kubernetes/K3s (MNP-003~005, decisions N2·N3·N6·N7).

Runs one fixed, allowlisted script in a short-lived Pod in the replication Namespace (same Pod
network as the replication workload), then a read-only `hostNetwork` Pod on the same node that
only runs `ip route get` to see the node's real egress (e.g. the WireGuard interface).
Credentials reach the Pod only through `secretKeyRef`; the script prints one JSON line of codes
and counters, never query output or credentials. Both Pods are deleted after every run.
"""

import contextlib
import json
import secrets
import time
from collections.abc import Callable
from subprocess import CompletedProcess

from cmp_contracts.network_preflight import NetworkPreflightRequest, NodeRoute, ProbeObservation

from cmp_providers.network_preflight import ProbeLocationUnavailable

Kubectl = Callable[..., CompletedProcess]
LABEL = {"app.kubernetes.io/managed-by": "cmp-network-preflight"}

# The only program the probe Pod runs. Inputs arrive as validated environment variables.
PROBE_SCRIPT = r"""
set -u
set -o pipefail
q() { if [ -n "$1" ]; then printf '"%s"' "$1"; else printf null; fi; }
emit() {
  printf '{"resolved_ip":%s,"stage":%s,"bytes":%s,"seconds":%s,"transfer":%s}\n' \
    "$(q "$1")" "$(q "$2")" "$3" "$4" "$(q "$5")"
}
IP=$(getent hosts "$DB_HOST" | awk '$1 ~ /^[0-9.]+$/ {print $1; exit}')
[ -z "$IP" ] && { emit "" DNS_FAILED 0 0 ""; exit 0; }
nc -z -w 5 "$IP" "$DB_PORT" >/dev/null 2>&1 || { emit "$IP" TCP_UNREACHABLE 0 0 ""; exit 0; }
CONN="host=$DB_HOST port=$DB_PORT dbname=$DB_NAME sslmode=$SSLMODE connect_timeout=10"
[ "$SSLMODE" = "verify-full" ] && CONN="$CONN sslrootcert=system"
ERR=$(psql "$CONN" -XAtq -c 'select 1' 2>&1 >/dev/null)
if [ $? -ne 0 ]; then
  case "$ERR" in
    *SSL*|*ssl*|*certificate*) S=TLS_FAILED ;;
    *authentication*|*password*|*pg_hba*|*role*) S=AUTH_FAILED ;;
    *timeout*|*"could not connect"*|*refused*) S=TCP_UNREACHABLE ;;
    *) S=QUERY_FAILED ;;
  esac
  emit "$IP" "$S" 0 0 ""; exit 0
fi
T0=$(cut -d' ' -f1 /proc/uptime)
N=$(timeout "$TIMEOUT" psql "$CONN" -XAtq -c "select repeat('x', $BYTES)" 2>/dev/null | wc -c)
RC=$?
T1=$(cut -d' ' -f1 /proc/uptime)
SEC=$(awk -v a="$T0" -v b="$T1" 'BEGIN { printf "%.3f", b - a }')
[ "$N" -gt 0 ] && N=$((N - 1))
if [ $RC -eq 124 ] || [ $RC -eq 143 ]; then ST=TIMEOUT; elif [ $RC -ne 0 ]; then ST=INTERRUPTED; else ST=COMPLETE; fi
emit "$IP" "" "$N" "$SEC" "$ST"
"""
ROUTE_SCRIPT = 'ip route get "$DEST_IP"'


def parse_route(line: str) -> NodeRoute:
    """`<ip> [via <gw>] dev <if> [src <addr>] ...` -> fields; anything else -> no route."""
    tokens = line.split()
    fields = {
        key: tokens[i + 1] for i, key in enumerate(tokens[:-1]) if key in ("via", "dev", "src")
    }
    return NodeRoute(dev=fields.get("dev"), via=fields.get("via"), src=fields.get("src"))


STAGES = {"DNS_FAILED", "TCP_UNREACHABLE", "TLS_FAILED", "AUTH_FAILED", "QUERY_FAILED"}


class KubernetesProbeExecutor:
    def __init__(
        self,
        kubectl: Kubectl,
        *,
        image: str,
        allowed_images: frozenset[str],
        poll_seconds: float = 2.0,
    ):
        if "@sha256:" not in image or image not in allowed_images:
            raise ValueError("probe image must be digest-pinned and allowlisted")
        self._kubectl, self._image, self._poll = kubectl, image, poll_seconds

    # -- Kubernetes helpers ---------------------------------------------------------------
    def _run(self, *args: str, content: bytes | None = None) -> CompletedProcess:
        try:
            return self._kubectl(*args, content=content)
        except Exception:  # noqa: BLE001 - cluster/Agent unreachable
            raise ProbeLocationUnavailable() from None

    def _create(self, pod: dict) -> bool:
        return self._run("create", "-f", "-", content=json.dumps(pod).encode()).returncode == 0

    def _wait(self, namespace: str, name: str, seconds: float) -> dict | None:
        deadline = time.monotonic() + seconds
        while True:
            got = self._run("get", "pod", name, "-n", namespace, "-o", "json")
            if got.returncode:
                raise ProbeLocationUnavailable()
            pod = json.loads(got.stdout)
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                return pod
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._poll)

    def _logs(self, namespace: str, name: str) -> str:
        got = self._run("logs", name, "-n", namespace)
        return got.stdout.decode(errors="replace") if got.returncode == 0 else ""

    def _delete(self, namespace: str, name: str) -> None:
        # Best effort: a Pod left behind carries the managed-by label for later cleanup.
        with contextlib.suppress(Exception):
            self._kubectl(
                "delete", "pod", name, "-n", namespace, "--ignore-not-found", "--wait=false"
            )

    # -- manifests --------------------------------------------------------------------------
    def probe_pod(self, request: NetworkPreflightRequest, name: str) -> dict:
        ref = request.db_auth_ref
        env = {
            "DB_HOST": request.source_db_host,
            "DB_PORT": str(request.source_db_port),
            "DB_NAME": request.database,
            "SSLMODE": request.tls_mode,
            "BYTES": str(request.transfer_bytes),
            "TIMEOUT": str(request.timeout_seconds),
        }
        spec = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 70, "runAsGroup": 70},
            "containers": [
                {
                    "name": "probe",
                    "image": self._image,
                    "command": ["/bin/sh", "-c", PROBE_SCRIPT],
                    "env": [{"name": k, "value": v} for k, v in env.items()]
                    + [
                        {
                            "name": "PGUSER",
                            "valueFrom": {"secretKeyRef": {"name": ref.secret_name, "key": ref.username_key}},
                        },
                        {
                            "name": "PGPASSWORD",
                            "valueFrom": {"secretKeyRef": {"name": ref.secret_name, "key": ref.password_key}},
                        },
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {"limits": {"cpu": "500m", "memory": "128Mi"}},
                }
            ],
        }  # fmt: skip
        if request.probe_target.node_name:
            spec["nodeName"] = request.probe_target.node_name
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": request.probe_target.namespace,
                "labels": LABEL,
            },
            "spec": spec,
        }

    def route_pod(self, namespace: str, name: str, node: str, dest_ip: str) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": LABEL},
            "spec": {
                "restartPolicy": "Never",
                "hostNetwork": True,
                "nodeName": node,
                "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 70, "runAsGroup": 70},
                "containers": [
                    {
                        "name": "route",
                        "image": self._image,
                        "command": ["/bin/sh", "-c", ROUTE_SCRIPT],
                        "env": [{"name": "DEST_IP", "value": dest_ip}],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
            },
        }

    # -- probe ------------------------------------------------------------------------------
    def _node_route(self, namespace: str, node: str | None, dest_ip: str) -> NodeRoute | None:
        if not node:
            return None
        name = f"cmp-preflight-route-{secrets.token_hex(4)}"
        try:
            if not self._create(self.route_pod(namespace, name, node, dest_ip)):
                return None  # e.g. hostNetwork not permitted: route stays UNKNOWN
            pod = self._wait(namespace, name, 60)
            if pod is None:
                return None
            text = self._logs(namespace, name)
            if pod["status"]["phase"] != "Succeeded":
                # `ip route get` exits non-zero when the node has no route at all.
                unreachable = "unreachable" in text.lower()
                return NodeRoute(dev=None, via=None, src=None) if unreachable else None
            lines = text.strip().splitlines()
            return parse_route(lines[0]) if lines else NodeRoute(dev=None, via=None, src=None)
        finally:
            self._delete(namespace, name)

    def probe(self, request: NetworkPreflightRequest) -> ProbeObservation:
        namespace = request.probe_target.namespace
        name = f"cmp-preflight-{secrets.token_hex(4)}"
        try:
            if not self._create(self.probe_pod(request, name)):
                raise ProbeLocationUnavailable()
            pod = self._wait(namespace, name, request.timeout_seconds + 90)
            if pod is None or pod["status"]["phase"] != "Succeeded":
                raise ProbeLocationUnavailable()
            node = pod.get("spec", {}).get("nodeName")
            lines = [
                line for line in self._logs(namespace, name).splitlines() if line.startswith("{")
            ]
            if not lines:
                raise ProbeLocationUnavailable()
            raw = json.loads(lines[-1])
        finally:
            self._delete(namespace, name)
        stage = raw.get("stage")
        transfer = None
        if not stage and raw.get("transfer"):
            transfer = {
                "bytes": int(raw["bytes"]),
                "seconds": float(raw["seconds"]),
                "status": raw["transfer"],
            }
        ip = raw.get("resolved_ip")
        return ProbeObservation(
            node=node,
            resolved_ip=ip,
            node_route=self._node_route(namespace, node, ip) if ip else None,
            db_stage_failed=stage if stage in STAGES else None,
            transfer=transfer,
            mtu={"probe": "NOT_ATTEMPTED"},
        )  # fmt: skip
