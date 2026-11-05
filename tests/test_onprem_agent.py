"""Ticket 12 Agent runner decisions at the rollout boundary, with an in-memory cluster double.

Real Helm/kubectl behavior is verified on isolated k3d (scripts/smoke_onprem_rollout.py); these
fast tests pin what the Agent may mutate: never on a mismatched/unprepared target, never a
re-install of already-applied bytes, never an automatic re-apply of an interrupted Release.
"""

import json
from subprocess import CompletedProcess

import pytest
from cmp_contracts.onprem_rollout import ProductionRolloutJob, rollout_job_id
from cmp_providers.artifacts import sha256_bytes
from cmp_providers.onprem_agent import (
    ENVIRONMENT,
    ClusterUnavailable,
    RolloutAgent,
    RolloutRunner,
)

CONTENT = {
    "db-chart": b"db chart tgz",
    "db-values": b'{"db":1}',
    "app-chart": b"app chart tgz",
    "app-values": b'{"app":1}',
}


def ref(name, kind):
    data = CONTENT[name]
    return {
        "id": name,
        "version": "1",
        "kind": kind,
        "sha256": sha256_bytes(data),
        "size_bytes": len(data),
    }


def job(**override):
    payload = {
        "mode": "PRODUCTION_ROLLOUT",
        "job_id": rollout_job_id("plan-k3s", 1),
        "approval_id": "approval-1",
        "plan_id": "plan-k3s",
        "revision": 1,
        "manifest_digest": "a" * 64,
        "environment_id": "onprem-prod-001",
        "agent_id": "agent-a",
        "cluster_uid": "cluster-a",
        "namespace": "demo",
        "storage_class": "local-path",
        "database": {
            "release": "demo-db",
            "chart": ref("db-chart", "CHART"),
            "values": ref("db-values", "VALUES"),
            "password_secret": {"name": "demo-db-auth", "key": "password"},
        },
        "application": {
            "release": "demo-app",
            "chart": ref("app-chart", "CHART"),
            "values": ref("app-values", "VALUES"),
            "port": 8080,
            "health_path": "/health",
            "secret_refs": [{"name": "demo-app-env", "key": "API_KEY"}],
        },
    }
    payload.update(override)
    return ProductionRolloutJob.model_validate(payload)


def ok(stdout=b""):
    return CompletedProcess([], 0, stdout, b"")


class FakeCluster:
    """Cluster double: Helm install makes workloads Ready unless `broken` says otherwise."""

    def __init__(self):
        self.cluster_uid = "cluster-a"
        self.helm_timeout = "1m"
        self.available = True
        self.db_probe = "160004|true|logical|10|10"
        self.broken = set()
        self.mutations = []
        self.releases = {}
        self.objects = {
            ("namespace", "kube-system"): {"metadata": {"uid": "cluster-a"}},
            ("storageclass", "local-path"): {"metadata": {"name": "local-path"}},
            ("namespace", "demo"): {"metadata": {"annotations": {ENVIRONMENT: "onprem-prod-001"}}},
            ("secret", "demo-app-env"): {"data": {"API_KEY": "c2VjcmV0"}},
        }

    def get(self, kind, name, namespace=None):
        if not self.available:
            raise ClusterUnavailable()
        return self.objects.get((kind, name))

    def apply(self, document):
        self.mutations.append(("apply", document["kind"]))
        self.objects[(document["kind"].lower(), document["metadata"]["name"])] = document

    def release(self, name, namespace):
        return self.releases.get(name)

    def helm(self, *args, timeout=60):
        assert args[:2] == ("upgrade", "--install")
        name, description = args[2], args[args.index("--description") + 1]
        self.mutations.append(("helm", name))
        failed = name in self.broken
        self.releases[name] = {
            "status": "failed" if failed else "deployed",
            "description": description,
        }
        if name == "demo-db":
            phase = "Pending" if failed else "Bound"
            self.objects[("pvc", "demo-db")] = {
                "spec": {"storageClassName": "local-path"},
                "status": {"phase": phase},
            }
        kind = "statefulset" if name == "demo-db" else "deployment"
        ready = 0 if failed else 1
        self.objects[(kind, name)] = {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"observedGeneration": 1, "readyReplicas": ready, "updatedReplicas": 1},
        }
        self.objects[("endpoints", name)] = {"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}
        return CompletedProcess([], 1 if failed else 0, b"", b"")

    def kubectl(self, *args, content=None, timeout=40):
        if "sh" in args:  # ticket 27 replication probe in the DB container
            if self.db_probe is None:
                return CompletedProcess([], 2, b"", b"psql: error")
            return ok(self.db_probe.encode())
        return ok()  # pg_isready and the Service-proxied health check succeed


def fetch(ref):
    return next(data for data in CONTENT.values() if sha256_bytes(data) == ref.sha256)


def run(cluster, rollout_job=None, fetcher=fetch):
    return RolloutRunner(cluster, fetcher).rollout(rollout_job or job())


def test_fresh_rollout_installs_db_then_app_and_reports_ready():
    cluster = FakeCluster()
    del cluster.objects[("namespace", "demo")]
    cluster.objects[("secret", "demo-app-env")] = None
    assert run(cluster, job(application=job().application.model_dump() | {"secret_refs": []})) == (
        "SUCCEEDED",
        "READY",
    )
    assert cluster.mutations == [
        ("apply", "Namespace"),
        ("apply", "Secret"),
        ("helm", "demo-db"),
        ("helm", "demo-app"),
    ]
    namespace = cluster.objects[("namespace", "demo")]
    assert namespace["metadata"]["annotations"] == {ENVIRONMENT: "onprem-prod-001"}


def test_reclaimed_job_with_applied_bytes_is_verified_without_reinstall():
    cluster = FakeCluster()
    assert run(cluster) == ("SUCCEEDED", "READY")
    cluster.mutations.clear()
    assert run(cluster) == ("SUCCEEDED", "READY")
    assert cluster.mutations == []


def test_interrupted_release_requires_inspection_and_is_not_reapplied():
    cluster = FakeCluster()
    cluster.releases["demo-db"] = {"status": "pending-install", "description": "x"}
    assert run(cluster) == ("UNKNOWN", "INSPECTION_REQUIRED")
    assert ("helm", "demo-db") not in cluster.mutations


@pytest.mark.parametrize(
    ("prepare", "code"),
    [
        (
            lambda c: c.objects.update({("secret", "demo-app-env"): None}),
            "SECRET_REFERENCE_MISSING",
        ),
        (
            lambda c: c.objects.update({("namespace", "demo"): {"metadata": {}}}),
            "NAMESPACE_NOT_OWNED",
        ),
        (
            lambda c: c.objects.update({("storageclass", "local-path"): None}),
            "STORAGE_CLASS_MISSING",
        ),
        (
            lambda c: c.objects.update(
                {("namespace", "kube-system"): {"metadata": {"uid": "other"}}}
            ),
            "CLUSTER_MISMATCH",
        ),
    ],
)
def test_unprepared_or_foreign_target_fails_before_any_mutation(prepare, code):
    cluster = FakeCluster()
    prepare(cluster)
    assert run(cluster) == ("FAILED", code)
    assert cluster.mutations == []


def test_tampered_artifact_bytes_fail_before_any_mutation():
    cluster = FakeCluster()
    assert run(cluster, fetcher=lambda ref: b"tampered") == ("FAILED", "ARTIFACT_DIGEST_MISMATCH")
    assert cluster.mutations == []


def test_failed_readiness_is_reported_with_the_failing_check_never_ready():
    cluster = FakeCluster()
    cluster.broken.add("demo-db")
    assert run(cluster) == ("FAILED", "PVC_NOT_BOUND")


class FakeClient:
    def __init__(self, payload):
        self.payload, self.reports = payload, []

    def poll_once(self, *, agent_id, credential):
        return {"job_id": self.payload["job_id"], "payload": self.payload, "status": "RUNNING"}

    def download_artifact(self, *, agent_id, credential, job_id, sha256):
        return next(d for d in CONTENT.values() if sha256_bytes(d) == sha256)

    def report(self, **kwargs):
        self.reports.append(kwargs)


def test_unreachable_cluster_keeps_job_running_without_report():
    cluster = FakeCluster()
    cluster.available = False
    client = FakeClient(job().model_dump(mode="json"))
    result = RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    assert result["status"] == "RUNNING" and client.reports == []


def test_agent_reports_only_codes():
    cluster = FakeCluster()
    client = FakeClient(job().model_dump(mode="json"))
    RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    [report] = client.reports
    assert report["status"] == "SUCCEEDED" and report["code"] == "READY"
    assert "c2VjcmV0" not in json.dumps(report)


def test_rollout_still_serving_old_pod_is_not_ready():
    """k3d finding: a stuck rolling update keeps the old Pod Ready (ready=1, updated=1)."""
    cluster = FakeCluster()
    cluster.broken.add("demo-app")
    original = cluster.helm

    def stuck(*args, timeout=60):
        result = original(*args, timeout=timeout)
        if args[2] == "demo-app":
            cluster.objects[("deployment", "demo-app")]["status"] = {
                "observedGeneration": 1,
                "replicas": 2,
                "readyReplicas": 1,
                "updatedReplicas": 1,
                "unavailableReplicas": 1,
            }
        return result

    cluster.helm = stuck
    assert run(cluster) == ("FAILED", "WORKLOAD_NOT_READY")


@pytest.mark.parametrize(
    ("probe", "replication"),
    [
        ("160004|true|logical|10|10", {"capable": True, "code": None}),
        ("160004|true|replica|10|10", {"capable": False, "code": "WAL_LEVEL_NOT_LOGICAL"}),
        (None, None),  # probe command failed: not verified, no field reported
    ],
)
def test_agent_reports_the_replication_check_with_ready(probe, replication):
    """Ticket 27: same SQL check as AWS/GCP; only the judged flag and code leave the Agent."""
    cluster = FakeCluster()
    cluster.db_probe = probe
    client = FakeClient(job().model_dump(mode="json"))
    RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    [report] = client.reports
    assert (report["status"], report["code"]) == ("SUCCEEDED", "READY")
    assert report.get("replication") == replication


def test_failed_rollout_reports_no_replication_check():
    cluster = FakeCluster()
    cluster.broken.add("demo-db")
    client = FakeClient(job().model_dump(mode="json"))
    RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    [report] = client.reports
    assert report["status"] == "FAILED" and "replication" not in report
