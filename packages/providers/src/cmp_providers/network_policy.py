"""`network_intent` → Kubernetes NetworkPolicy objects, shared by Sandbox and Production (ticket 26).

Common core (`base_policies`): default-deny for every Pod in the Namespace, DNS to kube-dns, and
one egress + one ingress policy per resource → resource intent, selected by the Helm release
label. Sandbox adds its egress-proxy rules (`cmp_worker.sandbox_network`); Production adds the
paths in `production_policies`:

- external entry (D-15): the application port is open to any source. The platform exposes the app
  through the provider LB/Ingress (ALB target-type ip, GKE NEG, K3s Traefik/API proxy), whose
  sources differ per provider and are not reliable L3 identities, so the entry is port-scoped,
  not source-scoped. Every other port and the database stay closed.
- `external_host` intents (no proxy in production): NetworkPolicy has no hostnames, so the source
  may egress to the intent ports on `0.0.0.0/0` except the private ranges (cluster, VPC, tunnel).
  Only created when such intents exist; the hostname itself is not enforced.
- platform paths the executing side needs: DB self-probe, DB replication egress (TCP 5432,
  Data Migration), and on ON_PREM the Agent tunnel → DB and the Network Preflight probe Pod.
- app telemetry (optional, `telemetry_cidr`): the applications may reach the platform's OTLP ingest
  (TCP 443 on that one CIDR, the demo host edge `/otlp/*`). Without it nothing changes.

Pure functions: no cluster access, deterministic output (sorted, hashed names).
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

INSTANCE = "app.kubernetes.io/instance"
PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
DB_PORT = 5432
# Production policies carry both executor ownership labels: 09/11 check `managed-by`, the ON_PREM
# Agent checks `cmp.dev/managed` (same convention as their Secrets).
PRODUCTION_LABELS = {"app.kubernetes.io/managed-by": "cmp-provisioning", "cmp.dev/managed": "true"}
# Feeds the Plan input Digest, so Revisions published before ticket 26 are re-planned.
TEMPLATE_VERSION = "network-policy-v1"
AGENT_POD = {"app.kubernetes.io/name": "cmp-onprem-agent"}
PREFLIGHT_POD = {"app.kubernetes.io/managed-by": "cmp-network-preflight"}


def selector(release: str) -> dict[str, Any]:
    return {"matchLabels": {INSTANCE: release}}


def policy(namespace: str, name: str, labels: dict[str, str], spec: dict[str, Any]) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
        "spec": spec,
    }


def policy_name(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]
    return f"cmp-allow-{digest}"


def default_deny(namespace: str, labels: dict[str, str]) -> dict:
    return policy(
        namespace,
        "cmp-default-deny",
        labels,
        {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    )


def allow_dns(namespace: str, labels: dict[str, str], *, exclude_label: str | None = None) -> dict:
    # DNS is open to every Pod by default. A Pod that carries `exclude_label` (the Sandbox red-team
    # executor, ticket 33) is left out: names of other apps must not be visible to it and DNS must
    # not become a way out. Without the argument the policy is exactly what it always was.
    pods: dict[str, Any] = (
        {}
        if exclude_label is None
        else {"matchExpressions": [{"key": exclude_label, "operator": "DoesNotExist"}]}
    )
    return policy(namespace, "cmp-allow-dns", labels, {
        "podSelector": pods, "policyTypes": ["Egress"],
        "egress": [{
            # GKE NodeLocal DNSCache(1.36 신규 클러스터 기본값, Dataplane V2)는 kube-dns Service로 가는
            # 질의를 노드의 node-local-dns Pod가 받는다. 둘 다 허용하지 않으면 Namespace 안 DNS가 모두 막힌다
            # (data-migration ticket 11 클라우드 검증에서 발견).
            "to": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": name}},
            } for name in ("kube-dns", "node-local-dns")],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        }],
    })  # fmt: skip


def intent_edges(
    namespace: str,
    labels: dict[str, str],
    releases: Mapping[str, str],
    intents: Sequence[Mapping[str, Any]],
) -> list[dict]:
    """Egress on the source and ingress on the destination for each resource → resource intent."""
    found: list[dict] = []
    for intent in intents:
        if not intent.get("destination_resource_id"):
            continue
        source = releases[intent["source_resource_id"]]
        destination = releases[intent["destination_resource_id"]]
        ports = [{"protocol": intent.get("protocol", "TCP"), "port": intent["port"]}]
        name = policy_name(source, destination, str(intent["port"]), ports[0]["protocol"])
        found.append(policy(namespace, name, labels, {
            "podSelector": selector(source), "policyTypes": ["Egress"],
            "egress": [{"to": [{"podSelector": selector(destination)}], "ports": ports}],
        }))  # fmt: skip
        found.append(policy(namespace, name + "-in", labels, {
            "podSelector": selector(destination), "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"podSelector": selector(source)}], "ports": ports}],
        }))  # fmt: skip
    return found


def entry(namespace: str, labels: dict[str, str], release: str, port: int) -> dict:
    """Any source may reach only the application port (test runners, LB/Ingress, API proxy)."""
    return policy(namespace, policy_name("entry", release), labels, {
        "podSelector": selector(release), "policyTypes": ["Ingress"],
        "ingress": [{"ports": [{"protocol": "TCP", "port": port}]}],
    })  # fmt: skip


def base_policies(
    namespace: str,
    labels: dict[str, str],
    releases: Mapping[str, str],
    intents: Sequence[Mapping[str, Any]],
    *,
    dns_exclude_label: str | None = None,
) -> list[dict]:
    return [
        default_deny(namespace, labels),
        allow_dns(namespace, labels, exclude_label=dns_exclude_label),
        *intent_edges(namespace, labels, releases, intents),
    ]


# --- Production -------------------------------------------------------------------------------


def _external_egress(namespace, labels, releases, intents) -> list[dict]:
    ports: dict[str, set[tuple[str, int]]] = {}
    for intent in intents:
        if intent.get("external_host"):
            source = releases[intent["source_resource_id"]]
            ports.setdefault(source, set()).add((intent.get("protocol", "TCP"), intent["port"]))
    found = []
    for source in sorted(ports):
        allowed = [{"protocol": p, "port": n} for p, n in sorted(ports[source])]
        found.append(policy(namespace, policy_name("external", source), labels, {
            "podSelector": selector(source), "policyTypes": ["Egress"],
            "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": PRIVATE}}],
                        "ports": allowed}],
        }))  # fmt: skip
    return found


def _telemetry_egress(namespace, labels, applications, cidr: str) -> list[dict]:
    found = []
    for app in sorted(applications):
        found.append(policy(namespace, policy_name("telemetry", app), labels, {
            "podSelector": selector(app), "policyTypes": ["Egress"],
            "egress": [{"to": [{"ipBlock": {"cidr": cidr}}],
                        "ports": [{"protocol": "TCP", "port": 443}]}],
        }))  # fmt: skip
    return found


def _database_paths(namespace, labels, database: str, provider: str) -> list[dict]:
    port = [{"protocol": "TCP", "port": DB_PORT}]
    sources: list[dict[str, Any]] = [{"podSelector": selector(database)}]
    if provider == "ON_PREM":
        # Ticket 17 tunnel forwarder runs in the Agent Pod (its own Namespace).
        sources.append({"namespaceSelector": {}, "podSelector": {"matchLabels": AGENT_POD}})
    return [
        policy(namespace, policy_name("db-in", database), labels, {
            "podSelector": selector(database), "policyTypes": ["Ingress"],
            "ingress": [{"from": sources, "ports": port}],
        }),
        # Self-probe through the Service (09/11) and the replication connection (Data Migration).
        policy(namespace, policy_name("db-out", database), labels, {
            "podSelector": selector(database), "policyTypes": ["Egress"],
            "egress": [{"ports": port}],
        }),
    ]  # fmt: skip


def with_connections(
    intents: Sequence[Mapping[str, Any]], connections: Sequence[tuple[str, str]]
) -> list[dict]:
    """Intents plus app → DB (TCP 5432) for each deployed DB binding, without duplicates.

    The platform wires the app to its target DB itself, so that edge never depends on the
    analysis having seen it in the source."""
    merged = [dict(i) for i in intents]
    seen = {
        (i["source_resource_id"], i.get("destination_resource_id"), i["port"], i.get("protocol"))
        for i in merged
    }
    for source, destination in connections:
        key = (source, destination, DB_PORT, "TCP")
        if key not in seen and (source, destination, DB_PORT, None) not in seen:
            seen.add(key)
            merged.append(
                {
                    "source_resource_id": source,
                    "destination_resource_id": destination,
                    "port": DB_PORT,
                    "protocol": "TCP",
                }
            )
    return merged


def production_policies(
    *,
    namespace: str,
    provider: str,
    releases: Mapping[str, str],
    applications: Mapping[str, int],
    databases: Sequence[str],
    intents: Sequence[Mapping[str, Any]],
    telemetry_cidr: str | None = None,
) -> list[dict]:
    """`releases`: resource id → release; `applications`: app release → port;
    `databases`: DB releases; `intents`: validated intents (see `with_connections`);
    `telemetry_cidr`: the platform OTLP ingest address, or None."""
    labels = PRODUCTION_LABELS
    found = base_policies(namespace, labels, releases, intents)
    found += [entry(namespace, labels, app, port) for app, port in sorted(applications.items())]
    found += _external_egress(namespace, labels, releases, intents)
    if telemetry_cidr:
        found += _telemetry_egress(namespace, labels, applications, telemetry_cidr)
    for database in sorted(databases):
        found += _database_paths(namespace, labels, database, provider)
    if provider == "ON_PREM":
        # MNP-01 probe Pod: one fixed script to the source DB address the caller gives.
        found.append(policy(namespace, "cmp-allow-preflight", labels, {
            "podSelector": {"matchLabels": PREFLIGHT_POD}, "policyTypes": ["Egress"],
            "egress": [{}],
        }))  # fmt: skip
    return found


def describe(
    *,
    releases: Mapping[str, str],
    applications: Mapping[str, int],
    databases: Sequence[str],
    intents: Sequence[Mapping[str, Any]],
    provider: str,
    telemetry_cidr: str | None = None,
) -> tuple[list[str], list[str]]:
    """(intent-derived lines, platform-default lines) for the Preview; names and ports only."""
    lines = ["Namespace 기본 차단(cmp-default-deny: 모든 Pod 수신·송신)"]
    for intent in intents:
        source = releases[intent["source_resource_id"]]
        protocol = intent.get("protocol", "TCP")
        if intent.get("destination_resource_id"):
            target = releases[intent["destination_resource_id"]]
            lines.append(f"{source} → {target} {protocol} {intent['port']} 허용")
        else:
            lines.append(
                f"{source} → 외부 {intent['external_host']} {protocol} {intent['port']} 허용"
                " (호스트명 미강제: 0.0.0.0/0 중 사설 대역 제외, 포트 제한)"
            )
    for app, port in sorted(applications.items()):
        lines.append(f"외부 진입(LB/Ingress) → {app} TCP {port} 허용, 다른 포트 차단")
    defaults = ["모든 Pod → kube-system kube-dns·node-local-dns UDP/TCP 53 허용"]
    if telemetry_cidr:
        for app in sorted(applications):
            defaults.append(
                f"{app} → 플랫폼 관측 수신(OpenTelemetry) {telemetry_cidr} TCP 443 허용"
            )
    for database in sorted(databases):
        defaults.append(f"{database} 자기 점검·복제 연결 TCP {DB_PORT} 허용(수신은 자신만)")
        if provider == "ON_PREM":
            defaults.append(f"Agent 터널 → {database} TCP {DB_PORT} 허용")
    if provider == "ON_PREM":
        defaults.append("Network Preflight 점검 Pod 송신 허용")
    return lines, defaults


# --- Enforcement detection (read-only, ticket 26) ---------------------------------------------


def aws_vpc_cni_enforces(aws_node: dict | None) -> bool:
    """EKS: the `aws-node` DaemonSet's `aws-eks-nodeagent` runs with network policy enabled
    (VPC CNI add-on `enableNetworkPolicy: "true"`, set by the 08 Terraform)."""
    containers = (((aws_node or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    for container in containers.get("containers") or []:
        if container.get("name") == "aws-eks-nodeagent":
            return "--enable-network-policy=true" in (container.get("args") or [])
    return False


def gke_dataplane_v2(anetd: dict | None) -> bool:
    """GKE: Dataplane V2 (`ADVANCED_DATAPATH`, set by the 10 Terraform) runs `anetd`."""
    return anetd is not None
