from concurrent.futures import ThreadPoolExecutor

import pytest
from cmp_contracts.agent import SandboxJob
from cmp_contracts.artifact import ArtifactRef
from cmp_providers.agent import JobStore
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes
from cmp_providers.k3s_agent import AgentError, K3sTarget, SandboxAgent, read_manifest


def test_poll_claim_survives_reconnect(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite")
    payload = {"job_id": "test-1", "agent_id": "agent-1"}
    assert store.submit(payload)["status"] == "QUEUED"
    assert store.claim("agent-1")["status"] == "RUNNING"
    reconnected = JobStore(tmp_path / "jobs.sqlite")
    assert reconnected.submit(payload)["status"] == "RUNNING"
    assert reconnected.claim("agent-1") is None
    with pytest.raises(ValueError):
        reconnected.submit({**payload, "agent_id": "other"})


@pytest.fixture
def job():
    ref = ArtifactRef(id="manifest", version="1", kind="MANIFEST", sha256="a" * 64, size_bytes=1)
    return SandboxJob(
        mode="SANDBOX_TEST",
        job_id="job-1",
        agent_id="agent-1",
        cluster_uid="cluster-1",
        namespace="cmp-test-one",
        namespace_uid="ns-1",
        storage_class="local-path",
        database_release="db",
        application_release="app",
        database_manifest=ref,
        application_manifest=ref,
        health_path="/health",
        application_port=8080,
    )


class Target:
    cluster_uid = "cluster-1"

    def deploy(self, job, root):
        return {"ready": True}

    def verify(self, job, root):
        return {"ready": True}


def test_successful_job_is_not_reissued(tmp_path, job):
    agent = SandboxAgent("agent-1", JobStore(tmp_path / "jobs"), Target(), tmp_path)
    agent.submit(job.model_dump(mode="json"))
    assert agent.poll_once()["status"] == "SUCCEEDED"
    assert agent.submit(job.model_dump(mode="json"))["status"] == "SUCCEEDED"
    assert agent.poll_once() is None
    restarted = SandboxAgent("agent-1", JobStore(tmp_path / "jobs"), Target(), tmp_path)
    assert restarted.reconnect(job.job_id)["result"] == {"ready": True}


def test_lost_ack_recovers_by_observation(tmp_path, job):
    store = JobStore(tmp_path / "jobs")
    agent = SandboxAgent("agent-1", store, Target(), tmp_path)
    agent.submit(job.model_dump(mode="json"))
    store.claim("agent-1")  # worker exited after the external side effect, before finish
    restarted = SandboxAgent("agent-1", JobStore(tmp_path / "jobs"), Target(), tmp_path)
    assert restarted.poll_once() is None
    assert restarted.reconnect(job.job_id)["status"] == "SUCCEEDED"


def test_uncertain_execution_is_not_retried(tmp_path, job):
    class Offline(Target):
        def deploy(self, job, root):
            raise AgentError("private command output")

        def verify(self, job, root):
            raise AgentError("private command output")

    agent = SandboxAgent("agent-1", JobStore(tmp_path / "jobs"), Offline(), tmp_path)
    agent.submit(job.model_dump(mode="json"))
    record = agent.poll_once()
    assert record["status"] == "UNKNOWN"
    assert record["result"] == {"code": "INSPECTION_REQUIRED"}
    assert agent.reconnect(job.job_id)["status"] == "UNKNOWN"
    assert agent.poll_once() is None


def test_claim_is_atomic(tmp_path):
    path = tmp_path / "jobs"
    store = JobStore(path)
    store.submit({"job_id": "one", "agent_id": "agent-1"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: JobStore(path).claim("agent-1"), range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_id", "other"),
        ("cluster_uid", "other"),
        ("mode", "PRODUCTION"),
        ("namespace", "default"),
    ],
)
def test_target_and_production_rejected(tmp_path, job, field, value):
    agent = SandboxAgent("agent-1", JobStore(tmp_path / "jobs"), Target(), tmp_path)
    payload = job.model_dump(mode="json")
    payload[field] = value
    with pytest.raises(AgentError):
        agent.submit(payload)
    assert agent.poll_once() is None


def test_explicit_context_required(tmp_path):
    path = tmp_path / "config"
    path.write_text("test")
    with pytest.raises(AgentError):
        K3sTarget(path, "minikube", "cluster-1")


@pytest.mark.parametrize("mutation", ["bytes", "namespace", "kind", "storage"])
def test_bad_artifacts_rejected(tmp_path, job, mutation):
    items = [
        {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {"name": "db", "namespace": job.namespace},
            "spec": {"storageClassName": "local-path"},
        }
        for kind in ["StatefulSet", "Service", "PersistentVolumeClaim"]
    ]
    if mutation == "namespace":
        items[0]["metadata"]["namespace"] = "default"
    elif mutation == "kind":
        items[0]["kind"] = "ClusterRole"
    elif mutation == "storage":
        items[2]["spec"]["storageClassName"] = "other"
    data = canonical_json_bytes({"apiVersion": "v1", "kind": "List", "items": items})
    ref = ArtifactRef(
        id="manifest", version="1", kind="MANIFEST", sha256=sha256_bytes(data), size_bytes=len(data)
    )
    (tmp_path / ref.sha256).write_bytes(data if mutation != "bytes" else b"tampered")
    with pytest.raises(ValueError):
        read_manifest(tmp_path, ref, job, "database")


def test_preflight_refuses_wrong_cluster(tmp_path, job):
    path = tmp_path / "config"
    path.write_text("test")
    target = K3sTarget(path, "k3d-cmp-test", "cluster-1")
    target.get = lambda *args: {"metadata": {"uid": "wrong-cluster"}}
    with pytest.raises(AgentError, match="CLUSTER_MISMATCH"):
        target.preflight(job)


def test_namespace_ownership_is_required(tmp_path, job):
    path = tmp_path / "config"
    path.write_text("test")
    target = K3sTarget(path, "k3d-cmp-test", "cluster-1")
    target.get = lambda kind, name: {
        "metadata": {"uid": "cluster-1" if name == "kube-system" else "ns-1", "labels": {}}
    }
    with pytest.raises(AgentError, match="NAMESPACE_OWNERSHIP_MISMATCH"):
        target.preflight(job)


def test_live_drift_not_hidden_by_api_defaults():
    from cmp_providers.k3s_agent import matches_desired

    desired = {"replicas": 1, "template": {"image": "app@sha256:fixed"}}
    assert matches_desired(desired, {**desired, "revisionHistoryLimit": 10})
    assert not matches_desired(desired, {**desired, "replicas": 2})
    assert not matches_desired(desired, {"replicas": 1, "template": {"image": "other"}})


def test_invalid_second_manifest_blocks_all_mutation(tmp_path, job):
    path = tmp_path / "config"
    path.write_text("test")
    target = K3sTarget(path, "k3d-cmp-test", "cluster-1")
    target.preflight = lambda job: {"connected": True}
    items = [
        {
            "kind": kind,
            "metadata": {"name": "db", "namespace": job.namespace},
            "spec": {"storageClassName": "local-path"},
        }
        for kind in ["StatefulSet", "Service", "PersistentVolumeClaim"]
    ]
    data = canonical_json_bytes({"kind": "List", "items": items})
    ref = ArtifactRef(
        id="db", version="1", kind="MANIFEST", sha256=sha256_bytes(data), size_bytes=len(data)
    )
    (tmp_path / ref.sha256).write_bytes(data)
    updated = job.model_copy(update={"database_manifest": ref})

    def forbidden(*args, **kwargs):
        pytest.fail("Kubernetes mutation happened before all artifacts were verified")

    target.run = forbidden
    with pytest.raises(OSError):
        target.deploy(updated, tmp_path)
