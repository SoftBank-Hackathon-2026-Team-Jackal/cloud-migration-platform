"""Opt-in real K3s check for ticket 26: production NetworkPolicies are enforced (kube-router).

Isolated k3d only (context `k3d-cmp-*`, dedicated kubeconfig; the default kubeconfig is never
used). Stand-in workloads (python:3.11-slim TCP listeners, imported with `k3d image import`) carry
the production chart labels. The policies are rendered by `production_policies` (ON_PREM) and
applied by the Agent's own `RolloutRunner` code path (managed-label conflict check, apply, prune).
The same connection matrix runs before (everything open) and after (only intended paths):

- allowed: app → DB 5432, other Namespace → app port (entry), host → Traefik Ingress → app,
  API server service proxy → app (Agent health check), DB → its own Service (self-probe), DNS.
- blocked: another Pod in the Namespace → DB, other Namespace → DB, app → another Pod, app →
  the Kubernetes API Service (private range).

Usage (cluster created beforehand, see ticket 26):
  uv run python scripts/smoke_network_policy_k3d.py --kubeconfig <file> --context k3d-cmp-t26 \
      --ingress-port 18026 --evidence docs/verification/prov-26-k3d.json
"""

import argparse
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from cmp_contracts.onprem_rollout import ProductionRolloutJob, rollout_job_id
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes
from cmp_providers.network_policy import production_policies, with_connections
from cmp_providers.onprem_agent import ClusterTarget, RolloutAgentError, RolloutRunner

NS, OTHER, CONFLICT_NS = "cmp-t26-prod", "cmp-t26-other", "cmp-t26-conflict"
IMAGE = "python:3.11-slim"
APP, DB, DEBUG = "api-main", "postgres-main", "debug"
SERVE = "import http.server as h,sys;h.ThreadingHTTPServer(('',int(sys.argv[1])),h.SimpleHTTPRequestHandler).serve_forever()"
CONNECT = "import socket,sys;socket.create_connection((sys.argv[1],int(sys.argv[2])),3).close()"


def kubectl(target: ClusterTarget, *args: str, content: bytes | None = None, check=True):
    result = target.kubectl(*args, content=content, timeout=120)
    if check and result.returncode:
        raise SystemExit(f"kubectl {args[:2]} failed: {result.stderr.decode()[:300]}")
    return result


def workload(namespace: str, name: str, port: int | None) -> list[dict]:
    labels = {"app.kubernetes.io/instance": name}
    command = ["python", "-c", SERVE, str(port)] if port else ["sleep", "3600"]
    items = [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "terminationGracePeriodSeconds": 1,
                "containers": [
                    {"name": "main", "image": IMAGE, "imagePullPolicy": "Never", "command": command}
                ],
            },
        }
    ]
    if port:
        items.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"selector": labels, "ports": [{"name": "http", "port": port}]},
            }
        )
    return items


def setup(target: ClusterTarget) -> None:
    docs = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": n}}
        for n in (NS, OTHER, CONFLICT_NS)
    ]
    docs += workload(NS, APP, 8080) + workload(NS, DB, 5432) + workload(NS, DEBUG, 8080)
    docs += workload(OTHER, "client", None)
    docs.append(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": APP, "namespace": NS},
            "spec": {
                "rules": [
                    {
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {"service": {"name": APP, "port": {"number": 8080}}},
                                }
                            ]
                        }
                    }
                ]
            },
        }
    )
    for doc in docs:
        target.apply(doc)
    for namespace, pod in [(NS, APP), (NS, DB), (NS, DEBUG), (OTHER, "client")]:
        for _ in range(36):
            ready = kubectl(target, "wait", "--for=condition=Ready", f"pod/{pod}", "-n",
                            namespace, "--timeout=5s", check=False)  # fmt: skip
            if ready.returncode == 0:
                break
        else:
            raise SystemExit(f"{namespace}/{pod} not Ready (image imported into k3d?)")


def connects(target, namespace, pod, host, port) -> bool:
    args = ["exec", "-n", namespace, pod, "--", "python", "-c", CONNECT, host, str(port)]
    return kubectl(target, *args, check=False).returncode == 0


def via_ingress(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


def via_api_proxy(target) -> bool:
    path = f"/api/v1/namespaces/{NS}/services/{APP}:http/proxy/"
    return kubectl(target, "get", "--raw", path, check=False).returncode == 0


def matrix(target: ClusterTarget, ingress_port: int) -> dict[str, bool]:
    svc = lambda name, ns=NS: f"{name}.{ns}.svc.cluster.local"
    return {
        "app_to_db_5432": connects(target, NS, APP, svc(DB), 5432),
        "db_to_own_service_5432": connects(target, NS, DB, svc(DB), 5432),
        "other_namespace_to_app_8080": connects(target, OTHER, "client", svc(APP), 8080),
        "traefik_ingress_to_app": via_ingress(ingress_port),
        "api_server_proxy_to_app": via_api_proxy(target),
        "debug_pod_to_db_5432": connects(target, NS, DEBUG, svc(DB), 5432),
        "other_namespace_to_db_5432": connects(target, OTHER, "client", svc(DB), 5432),
        "app_to_debug_pod_8080": connects(target, NS, APP, svc(DEBUG), 8080),
        "app_to_kubernetes_api_443": connects(target, NS, APP, "10.43.0.1", 443),
    }


EXPECTED = {
    "app_to_db_5432": True,
    "db_to_own_service_5432": True,
    "other_namespace_to_app_8080": True,
    "traefik_ingress_to_app": True,
    "api_server_proxy_to_app": True,
    "debug_pod_to_db_5432": False,
    "other_namespace_to_db_5432": False,
    "app_to_debug_pod_8080": False,
    "app_to_kubernetes_api_443": False,
}


def policies_for(namespace: str) -> list[dict]:
    return production_policies(
        namespace=namespace,
        provider="ON_PREM",
        releases={APP: APP, DB: DB},
        applications={APP: 8080},
        databases=[DB],
        intents=with_connections([], [(APP, DB)]),
    )


def agent_job(namespace: str, content: bytes) -> ProductionRolloutJob:
    def ref(name, kind, data=b"x"):
        return {"id": name, "version": "1", "kind": kind, "sha256": sha256_bytes(data),
                "size_bytes": len(data)}  # fmt: skip

    return ProductionRolloutJob.model_validate(
        {
            "mode": "PRODUCTION_ROLLOUT",
            "job_id": rollout_job_id("plan-t26", 1),
            "approval_id": "approval-t26",
            "plan_id": "plan-t26",
            "revision": 1,
            "manifest_digest": "a" * 64,
            "environment_id": "onprem-t26",
            "agent_id": "agent-t26",
            "cluster_uid": "uid",
            "namespace": namespace,
            "storage_class": "local-path",
            "database": {"release": DB, "chart": ref("c", "CHART"), "values": ref("v", "VALUES"),
                         "password_secret": {"name": f"{DB}-auth", "key": "password"}},
            "application": {"release": APP, "chart": ref("c", "CHART"),
                            "values": ref("v", "VALUES"), "port": 8080, "health_path": "/"},
            "network_policies": ref("network_policies", "MANIFEST", content),
        }
    )  # fmt: skip


def agent_apply(target: ClusterTarget, namespace: str, workdir: Path) -> None:
    """The Agent's own read-only check + apply + prune (RolloutRunner, ticket 26)."""
    content = canonical_json_bytes({"policies": policies_for(namespace)})
    job = agent_job(namespace, content)
    path = workdir / f"{namespace}.json"
    path.write_bytes(content)
    runner = RolloutRunner(target, fetch=None)
    policies = runner._network_policies(job, {job.network_policies.sha256: path})
    runner._apply_network_policies(job, policies)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--ingress-port", type=int, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if not args.context.startswith("k3d-cmp-"):
        raise SystemExit("isolated k3d context (k3d-cmp-*) required")
    target = ClusterTarget(args.kubeconfig, args.context, "")
    uid = target.discover_cluster_uid()
    setup(target)
    for _ in range(30):  # Traefik picks the Ingress up asynchronously
        if via_ingress(args.ingress_port):
            break
        time.sleep(2)
    before = matrix(target, args.ingress_port)

    # A managed policy from an "earlier Revision" must be pruned; an unmanaged same-name policy
    # in another Namespace must block the Agent before any change there.
    stale = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
             "metadata": {"name": "cmp-allow-old", "namespace": NS,
                          "labels": {"cmp.dev/managed": "true"}},
             "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{}]}}  # fmt: skip
    foreign = {**stale, "metadata": {"name": "cmp-default-deny", "namespace": CONFLICT_NS}}
    target.apply(stale)
    target.apply(foreign)
    with tempfile.TemporaryDirectory(prefix="cmp-t26-") as tmp:
        agent_apply(target, NS, Path(tmp))
        try:
            agent_apply(target, CONFLICT_NS, Path(tmp))
            conflict = "APPLIED"
        except RolloutAgentError as error:
            conflict = error.code
    managed = kubectl(target, "get", "networkpolicy", "-n", NS, "-o", "json")
    names = sorted(i["metadata"]["name"] for i in json.loads(managed.stdout)["items"])
    conflict_ns = kubectl(target, "get", "networkpolicy", "-n", CONFLICT_NS, "-o", "name")
    time.sleep(5)  # kube-router programs the rules asynchronously
    after = matrix(target, args.ingress_port)

    evidence = {
        "ticket": "26",
        "date": time.strftime("%Y-%m-%d"),
        "context": args.context,
        "cluster_uid": uid,
        "k3s_version": json.loads(kubectl(target, "version", "-o", "json").stdout)["serverVersion"][
            "gitVersion"
        ],
        "enforcement": "k3s built-in network policy controller (kube-router)",
        "before_policies": before,
        "after_policies": after,
        "expected_after": EXPECTED,
        "applied_policies": names,
        "stale_managed_policy_pruned": "cmp-allow-old" not in names,
        "unmanaged_same_name_conflict": conflict,
        "conflict_namespace_untouched": conflict_ns.stdout.decode().split()
        == ["networkpolicy.networking.k8s.io/cmp-default-deny"],
    }
    evidence["passed"] = (
        all(before.values())
        and after == EXPECTED
        and evidence["stale_managed_policy_pruned"]
        and conflict == "NETWORK_POLICY_CONFLICT"
        and evidence["conflict_namespace_untouched"]
    )
    text = json.dumps(evidence, indent=2, sort_keys=True)
    print(text)
    if args.evidence:
        args.evidence.write_text(text + "\n")
    sys.exit(0 if evidence["passed"] else 1)


if __name__ == "__main__":
    main()
