"""Ticket 26: production NetworkPolicies from the approved `network_intent`.

Seams: the shared conversion (`cmp_providers.network_policy`) judged by a small NetworkPolicy
evaluator (allowed paths pass, everything else is blocked), the published Plan/Preview, the
AWS/GCP deploy Activity and the ON_PREM Agent. Real enforcement on K3s is checked separately by
`scripts/smoke_network_policy_k3d.py` (evidence in `docs/verification/prov-26-k3d.json`).
"""

import ipaddress
import json
from subprocess import CompletedProcess

import pytest
import test_cloud_workload_plan as cwp
import test_onprem_agent as agent_tests
from cmp_api.onprem_rollout import build_rollout_job
from cmp_contracts.onprem_rollout import ProductionRolloutJob
from cmp_providers.artifacts import sha256_bytes
from cmp_providers.network_policy import (
    PRODUCTION_LABELS,
    aws_vpc_cni_enforces,
    base_policies,
    describe,
    gke_dataplane_v2,
    production_policies,
    with_connections,
)
from cmp_providers.onprem_agent import RolloutRunner
from cmp_worker.sandbox_network import build_manifests
from network_policy_support import AWS_NODE
from onprem_rollout_support import register_agent
from temporalio.testing import ActivityEnvironment
from test_plan_publish import (
    MIGRATION,
    publisher,
    register,
    registration,
    stored,
)
from test_provisioning_v2 import _requirements

# Ticket 18 fixtures (SQLite business DB, local ArtifactStore).
db, store = cwp.db, cwp.store

NS = "prod"
RELEASES = {"api-main": "api-main", "postgres-main": "postgres-main"}
EXTERNAL = {"source_resource_id": "api-main", "external_host": "api.pay.example.com", "port": 443}


# --- a minimal NetworkPolicy evaluator ---------------------------------------------------------


def _matches(selector: dict, labels: dict) -> bool:
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        assert expression["operator"] == "In"
        if labels.get(expression["key"]) not in expression["values"]:
            return False
    return True


def _peer(peer: dict, owner_ns: str, other: dict) -> bool:
    if "ipBlock" in peer:
        if other.get("pod"):
            return False  # pod IPs are matched by selectors in this model
        ip = ipaddress.ip_address(other["ip"])
        block = peer["ipBlock"]
        return ip in ipaddress.ip_network(block["cidr"]) and not any(
            ip in ipaddress.ip_network(c) for c in block.get("except", [])
        )
    if not other.get("pod"):
        return False
    if "namespaceSelector" in peer:
        ns_ok = _matches(peer["namespaceSelector"], {"kubernetes.io/metadata.name": other["ns"]})
    else:
        ns_ok = other["ns"] == owner_ns
    return ns_ok and _matches(peer.get("podSelector") or {}, other["labels"])


def _side(policies, pod, other, port, protocol, kind) -> bool:
    if not pod.get("pod"):
        return True  # outside the cluster: no policy applies
    chosen = [
        p
        for p in policies
        if p["metadata"]["namespace"] == pod["ns"]
        and kind in p["spec"]["policyTypes"]
        and _matches(p["spec"]["podSelector"], pod["labels"])
    ]
    if not chosen:
        return True
    field, peers_key = ("egress", "to") if kind == "Egress" else ("ingress", "from")
    for item in chosen:
        for rule in item["spec"].get(field) or []:
            peers = rule.get(peers_key)
            ports = rule.get("ports")
            peer_ok = not peers or any(_peer(p, pod["ns"], other) for p in peers)
            port_ok = not ports or any(
                (x.get("protocol", "TCP"), x["port"]) == (protocol, port) for x in ports
            )
            if peer_ok and port_ok:
                return True
    return False


def allowed(policies, source, destination, port, protocol="TCP") -> bool:
    return _side(policies, source, destination, port, protocol, "Egress") and _side(
        policies, destination, source, port, protocol, "Ingress"
    )


def pod(ns, **labels):
    return {"pod": True, "ns": ns, "labels": labels}


APP = pod(NS, **{"app.kubernetes.io/instance": "api-main"})
DB = pod(NS, **{"app.kubernetes.io/instance": "postgres-main"})
INTRUDER = pod(NS, **{"app.kubernetes.io/instance": "debug"})
OTHER_NS = pod("other", **{"app.kubernetes.io/instance": "api-main"})
AGENT = pod("cmp-agent", **{"app.kubernetes.io/name": "cmp-onprem-agent"})
PREFLIGHT = pod(NS, **{"app.kubernetes.io/managed-by": "cmp-network-preflight"})
DNS = pod("kube-system", **{"k8s-app": "kube-dns"})
NODE_LOCAL_DNS = pod("kube-system", **{"k8s-app": "node-local-dns"})  # GKE NodeLocal DNSCache
LB = {"ip": "10.0.1.5"}  # ALB ENI / NEG / Traefik source: not a selector identity
INTERNET = {"ip": "93.184.216.34"}
VPC_HOST = {"ip": "10.0.5.5"}


def rules(provider="AWS", intents=None):
    intents = [] if intents is None else intents
    return production_policies(
        namespace=NS,
        provider=provider,
        releases=RELEASES,
        applications={"api-main": 8080},
        databases=["postgres-main"],
        intents=with_connections(intents, [("api-main", "postgres-main")]),
    )


# --- shared conversion -------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["AWS", "GCP", "ON_PREM"])
def test_only_the_intended_paths_pass_and_everything_else_is_blocked(provider):
    policies = rules(provider, [EXTERNAL])
    # Allowed: app → DB, LB/Ingress → app port, DNS, the external intent, replication.
    assert allowed(policies, APP, DB, 5432)
    assert allowed(policies, LB, APP, 8080) and allowed(policies, INTERNET, APP, 8080)
    assert allowed(policies, APP, DNS, 53, "UDP") and allowed(policies, DB, DNS, 53, "UDP")
    assert allowed(policies, APP, NODE_LOCAL_DNS, 53, "UDP")
    assert allowed(policies, DB, NODE_LOCAL_DNS, 53, "TCP")
    assert allowed(policies, APP, INTERNET, 443)
    assert allowed(policies, DB, VPC_HOST, 5432)  # Data Migration replication connection
    assert allowed(policies, DB, DB, 5432)  # 09/11 self-probe through the Service
    # Blocked: anything the intent does not name.
    assert not allowed(policies, INTRUDER, DB, 5432)
    assert not allowed(policies, OTHER_NS, DB, 5432)
    assert not allowed(policies, LB, DB, 5432) and not allowed(policies, INTERNET, DB, 5432)
    assert not allowed(policies, LB, APP, 9090)
    assert not allowed(policies, APP, INTERNET, 22)
    assert not allowed(policies, APP, VPC_HOST, 443)  # private ranges stay closed
    assert not allowed(policies, DB, INTERNET, 443)
    assert not allowed(policies, APP, INTRUDER, 8080)
    assert not allowed(policies, DB, APP, 8080)
    # ON_PREM only: the Agent tunnel reaches the DB and the preflight probe may leave.
    assert allowed(policies, AGENT, DB, 5432) == (provider == "ON_PREM")
    assert allowed(policies, PREFLIGHT, VPC_HOST, 6432) == (provider == "ON_PREM")


def test_external_egress_exists_only_for_external_intents():
    policies = rules("AWS")
    assert not allowed(policies, APP, INTERNET, 443)
    assert not [p for p in policies if "ipBlock" in json.dumps(p)]


PLATFORM_EDGE = {"ip": "13.209.240.49"}


@pytest.mark.parametrize("provider", ["AWS", "GCP", "ON_PREM"])
def test_telemetry_egress_opens_only_the_platform_ingest_on_443(provider):
    policies = production_policies(
        namespace=NS,
        provider=provider,
        releases=RELEASES,
        applications={"api-main": 8080},
        databases=["postgres-main"],
        intents=with_connections([], [("api-main", "postgres-main")]),
        telemetry_cidr="13.209.240.49/32",
    )
    assert allowed(policies, APP, PLATFORM_EDGE, 443)
    assert not allowed(policies, APP, PLATFORM_EDGE, 80)
    assert not allowed(policies, APP, INTERNET, 443)  # only that one address
    assert not allowed(policies, DB, PLATFORM_EDGE, 443)  # applications only
    # Without the setting the policies are exactly what they were.
    before = rules(provider)
    assert len([p for p in policies if p not in before]) == 1
    assert all(p in policies for p in before)


def test_preview_lists_the_telemetry_egress_as_a_platform_default():
    shape = {
        "releases": RELEASES,
        "applications": {"api-main": 8080},
        "databases": ["postgres-main"],
        "intents": [],
        "provider": "ON_PREM",
    }
    _, without = describe(**shape)
    _, with_telemetry = describe(**shape, telemetry_cidr="13.209.240.49/32")
    assert [line for line in with_telemetry if line not in without] == [
        "api-main → 플랫폼 관측 수신(OpenTelemetry) 13.209.240.49/32 TCP 443 허용"
    ]


def test_app_to_db_is_allowed_even_when_analysis_saw_no_edge():
    assert allowed(rules("AWS", []), APP, DB, 5432)
    assert rules("AWS", []) == rules("AWS", [_requirements()["network_intent"][0]])


def test_policies_are_deterministic_and_platform_managed():
    first, second = rules("GCP", [EXTERNAL]), rules("GCP", [EXTERNAL])
    assert json.dumps(first) == json.dumps(second)
    assert all(p["metadata"]["labels"] == PRODUCTION_LABELS for p in first)
    names = [p["metadata"]["name"] for p in first]
    assert len(names) == len(set(names)) and names[0] == "cmp-default-deny"


def test_sandbox_and_production_share_the_same_conversion():
    intents = with_connections([EXTERNAL], [("api-main", "postgres-main")])
    sandbox = build_manifests(
        namespace=NS,
        labels=PRODUCTION_LABELS,
        resources=[
            {"kind": "APPLICATION", "release_name": "api-main", "port": 8080},
            {"kind": "POSTGRESQL", "release_name": "postgres-main"},
        ],
        releases=RELEASES,
        intents=intents,
        allow=[],
        proxy_script="",
    )
    shared = base_policies(NS, PRODUCTION_LABELS, RELEASES, intents)
    assert sandbox[: len(shared)] == shared
    assert rules("AWS", [EXTERNAL])[: len(shared)] == shared
    # Production entry is the Sandbox entry rule (D-15: port-scoped, any source).
    entry = sandbox[len(shared)]
    assert entry in rules("AWS", [EXTERNAL])


def test_enforcement_detection_reads_the_target_engines():
    assert aws_vpc_cni_enforces(AWS_NODE)
    off = json.loads(json.dumps(AWS_NODE).replace("=true", "=false"))
    assert not aws_vpc_cni_enforces(off) and not aws_vpc_cni_enforces(None)
    assert gke_dataplane_v2({"metadata": {"name": "anetd"}}) and not gke_dataplane_v2(None)


# --- Plan / Preview -----------------------------------------------------------------------------


def _policy_bytes(db, store, result) -> list[dict]:
    manifest, _ = stored(db, result)
    ref = manifest.artifacts["network_policies"]
    assert ref.kind == "MANIFEST"
    return json.loads(store.read(ref))["policies"]


def _intent_requirements():
    requirements = _requirements()
    requirements["network_intent"].append(EXTERNAL)
    return requirements


def test_onprem_plan_pins_policies_in_the_digest_and_preview_shows_them(db, store, tmp_path):
    with db() as session:
        register_agent(session)
    register(db, store, registration(requirements=_intent_requirements())[0])
    result = publisher(db, store, tmp_path).publish(MIGRATION)
    assert result["status"] == "PUBLISHED", result
    policies = _policy_bytes(db, store, result)
    assert {p["metadata"]["namespace"] for p in policies} == {"cmp-prod-demo"}
    by_selector = [p for p in policies if "ipBlock" in json.dumps(p)]
    assert by_selector and by_selector[0]["spec"]["egress"][0]["ports"] == [
        {"protocol": "TCP", "port": 443}
    ]
    _, preview = stored(db, result)
    network = [c.name for c in preview.changes if c.resource_type == "NETWORK"]
    assert any("api-main → postgres-main TCP 5432" in n for n in network)
    assert any("api.pay.example.com" in n for n in network)
    assert any("외부 진입" in n for n in network)
    defaults = [c.name for c in preview.applied_defaults if c.resource_type == "NETWORK"]
    assert any("Agent 터널" in n for n in defaults)

    # A changed intent is a new input → a new Revision; the earlier approval goes stale.
    register(db, store, registration()[0])
    again = publisher(db, store, tmp_path).publish(MIGRATION)
    assert again["revision"] == result["revision"] + 1
    assert not [p for p in _policy_bytes(db, store, again) if "ipBlock" in json.dumps(p)]


@pytest.fixture
def platform_registry(monkeypatch):
    """Ticket 29: an ON_PREM job with an ECR image needs the platform registry configured."""
    from cmp_api.config import settings

    monkeypatch.setattr(
        settings, "platform_ecr_registry", "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"
    )
    monkeypatch.setattr(settings, "platform_ecr_repositories", "demo")


@pytest.mark.usefixtures("platform_registry")
def test_onprem_rollout_job_carries_the_approved_policy_artifact(db, store, tmp_path):
    with db() as session:
        register_agent(session)
    register(db, store, registration()[0])
    result = publisher(db, store, tmp_path).publish(MIGRATION)
    claim = cwp.approve(db, result)
    with db() as session:
        job, _ = build_rollout_job(session, {**claim, "provider": "ON_PREM"}, store)
    manifest, _ = stored(db, result)
    assert job.network_policies == manifest.artifacts["network_policies"]
    assert job.network_policies in job.artifact_refs()


@pytest.mark.parametrize("provider", ["AWS", "GCP"])
def test_cloud_workload_plan_pins_policies_and_preview_lists_them(
    db, store, tmp_path, monkeypatch, provider
):
    pub, infra = cwp.infra_ready(db, store, tmp_path, monkeypatch, provider)
    published = pub.publish_workloads(MIGRATION, infra)
    manifest, preview = stored(db, published)
    folder = tmp_path / "plans" / f"{published['plan_id']}-r{published['revision']}"
    ref = manifest.artifacts["network_policies"]
    policies = json.loads((folder / ref.sha256).read_bytes())["policies"]
    assert {p["metadata"]["namespace"] for p in policies} == {"cmp-prod-demo"}
    assert "Agent" not in json.dumps(preview.model_dump())
    assert any(c.resource_type == "NETWORK" for c in preview.changes)


# --- AWS/GCP deploy Activity -------------------------------------------------------------------


async def _deploy(db, store, tmp_path, monkeypatch, provider, prepare=None):
    pub, infra = cwp.infra_ready(db, store, tmp_path, monkeypatch, provider)
    published = pub.publish_workloads(MIGRATION, infra)
    claim = cwp.approve(db, published)
    cluster, execute = cwp.workload_activity(db, tmp_path, provider)
    if prepare:
        prepare(cluster)
    result = await ActivityEnvironment().run(execute, {**claim, "resource_refs": {}})
    return cluster, result


def _events(cluster):
    return [json.loads(line) for line in cluster.trace]


@pytest.mark.parametrize("provider", ["AWS", "GCP"])
async def test_policies_are_applied_before_helm_and_reported(
    db, store, tmp_path, monkeypatch, provider
):
    cluster, result = await _deploy(db, store, tmp_path, monkeypatch, provider)
    assert result["status"] == "SUCCEEDED", result
    events = _events(cluster)
    first_policy = next(
        i for i, e in enumerate(events) if e[0] == "apply" and e[1]["kind"] == "NetworkPolicy"
    )
    first_helm = next(i for i, e in enumerate(events) if e[0] == "helm")
    assert first_policy < first_helm
    applied = [m for m in cluster.applied if m["kind"] == "NetworkPolicy"]
    refs = result["resource_refs"]
    assert refs["network_policies"].split(",") == sorted(m["metadata"]["name"] for m in applied)
    engine = {"AWS": "aws-vpc-cni", "GCP": "gke-dataplane-v2"}[provider]
    assert refs["network_policy_enforcement"] == engine


@pytest.mark.parametrize("provider", ["AWS", "GCP"])
async def test_cluster_without_enforcement_is_refused_before_any_workload(
    db, store, tmp_path, monkeypatch, provider
):
    def disable(cluster):
        cluster.enforcing = False

    cluster, result = await _deploy(db, store, tmp_path, monkeypatch, provider, disable)
    assert (result["status"], result["code"]) == ("FAILED", "NETWORK_POLICY_NOT_ENFORCED")
    assert cluster.helm_calls == []
    assert not [m for m in cluster.applied if m["kind"] == "NetworkPolicy"]


async def test_unmanaged_same_name_policy_is_a_conflict(db, store, tmp_path, monkeypatch):
    def foreign(cluster):
        cluster.existing_policies = {"cmp-default-deny": False}

    cluster, result = await _deploy(db, store, tmp_path, monkeypatch, "AWS", foreign)
    assert (result["status"], result["code"]) == ("FAILED", "NETWORK_POLICY_CONFLICT")
    assert cluster.helm_calls == []
    assert not [m for m in cluster.applied if m["kind"] == "NetworkPolicy"]


async def test_managed_policy_no_longer_approved_is_removed(db, store, tmp_path, monkeypatch):
    def stale(cluster):
        cluster.existing_policies = {"cmp-allow-old": True, "user-policy": False}

    cluster, result = await _deploy(db, store, tmp_path, monkeypatch, "GCP", stale)
    assert result["status"] == "SUCCEEDED", result
    deleted = [e for e in _events(cluster) if e[0] == "delete-networkpolicy"]
    assert deleted == [["delete-networkpolicy", "cmp-prod-demo", "cmp-allow-old"]]
    assert cluster.existing_policies == {"user-policy": False}  # never touched


# --- ON_PREM Agent -----------------------------------------------------------------------------

POLICIES = rules("ON_PREM", [EXTERNAL])


def _agent_job(namespace="demo"):
    for item in POLICIES:
        item["metadata"]["namespace"] = namespace
    content = json.dumps({"policies": POLICIES}).encode()
    agent_tests.CONTENT["policies"] = content
    ref = {
        "id": "policies",
        "version": "1",
        "kind": "MANIFEST",
        "sha256": sha256_bytes(content),
        "size_bytes": len(content),
    }
    base = agent_tests.job().model_dump(mode="json")
    return ProductionRolloutJob.model_validate({**base, "network_policies": ref})


class PolicyCluster(agent_tests.FakeCluster):
    def __init__(self):
        super().__init__()
        self.deleted = []

    def kubectl(self, *args, content=None, timeout=40):
        if args[:2] == ("get", "networkpolicy"):
            items = [
                {"metadata": {"name": name}}
                for (kind, name), item in self.objects.items()
                if kind == "networkpolicy"
                and (item["metadata"].get("labels") or {}).get("cmp.dev/managed") == "true"
            ]
            return CompletedProcess([], 0, json.dumps({"items": items}).encode(), b"")
        if args[:2] == ("delete", "networkpolicy"):
            self.deleted.append(args[2])
            self.objects.pop(("networkpolicy", args[2]))
        return agent_tests.ok()


def test_agent_applies_policies_after_namespace_and_before_helm():
    cluster = PolicyCluster()
    job = _agent_job()
    assert RolloutRunner(cluster, agent_tests.fetch).rollout(job) == ("SUCCEEDED", "READY")
    kinds = [kind for _, kind in cluster.mutations]
    first_policy, first_helm = (
        kinds.index("NetworkPolicy"),
        cluster.mutations.index(("helm", "demo-db")),
    )
    assert first_policy < first_helm
    assert kinds.count("NetworkPolicy") == len(POLICIES)


def test_agent_refuses_unmanaged_same_name_policy_before_any_mutation():
    cluster = PolicyCluster()
    cluster.objects[("networkpolicy", "cmp-default-deny")] = {"metadata": {"labels": {}}}
    result = RolloutRunner(cluster, agent_tests.fetch).rollout(_agent_job())
    assert result == ("FAILED", "NETWORK_POLICY_CONFLICT")
    assert cluster.mutations == []


def test_agent_rejects_policies_for_another_namespace():
    cluster = PolicyCluster()
    result = RolloutRunner(cluster, agent_tests.fetch).rollout(_agent_job(namespace="elsewhere"))
    assert result == ("FAILED", "INVALID_NETWORK_POLICIES")
    assert cluster.mutations == []


def test_agent_removes_managed_policies_the_revision_dropped():
    cluster = PolicyCluster()
    cluster.objects[("networkpolicy", "cmp-allow-old")] = {
        "metadata": {"labels": {"cmp.dev/managed": "true"}}
    }
    assert RolloutRunner(cluster, agent_tests.fetch).rollout(_agent_job()) == (
        "SUCCEEDED",
        "READY",
    )
    assert cluster.deleted == ["cmp-allow-old"]


def test_cli_reads_policy_ownership_and_deletes_by_name(tmp_path):
    from cmp_worker.activities.aws_workloads_cli import CliRuntime, KubectlSession

    calls = []
    listed = {
        "items": [
            {"metadata": {"name": "cmp-default-deny", "labels": PRODUCTION_LABELS}},
            {"metadata": {"name": "user-policy"}},
        ]
    }

    class Recording(CliRuntime):
        def run(self, args, *, stdin=None, timeout=300):
            calls.append(args)
            return json.dumps(listed).encode() if "get" in args else b""

    session = KubectlSession(Recording(), tmp_path / "kubeconfig", tmp_path)
    assert session.network_policies("ns") == {"cmp-default-deny": True, "user-policy": False}
    session.delete_network_policy("ns", "cmp-allow-old")
    assert calls[-1][-6:] == ["delete", "networkpolicy", "cmp-allow-old", "-n", "ns",
                              "--ignore-not-found"]  # fmt: skip
