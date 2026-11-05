"""Ticket 51 (audit C-02): a failed ON_PREM Revision keeps the running app and DB, and the next
Revision can still be applied.

The cluster double keeps Helm history and models the k3d finding that a StatefulSet whose new
Pod never schedules stays stuck on that Pod even after its spec is rolled back, until the Pod is
deleted. Real Helm/kubectl behavior is measured on k3d (scripts/smoke_onprem_rollout.py).
"""

import json
from subprocess import CompletedProcess

import pytest
from cmp_contracts.onprem_rollout import rollout_job_id
from cmp_providers.agent_rollout_recovery import duration_seconds
from cmp_providers.onprem_agent import ClusterTarget, RolloutAgent
from cmp_providers.provisioning_result import _checks
from test_onprem_agent import FakeClient, FakeCluster, job, run


def revision_job(revision):
    return job(job_id=rollout_job_id("plan-k3s", revision), revision=revision)


def done(code=0):
    return CompletedProcess([], code, b"", b"")


class HistoryCluster(FakeCluster):
    """Helm 4 without `--wait`: an upgrade is recorded `deployed` at once; whether the workload
    settles is decided by `broken`. A broken StatefulSet leaves a Pending Pod of its revision."""

    def __init__(self):
        super().__init__()
        self.revisions: dict[str, list[dict]] = {}
        self.pods: dict[str, dict] = {}  # StatefulSet name -> its stuck Pod
        self.rollback_fails = False

    # --- Helm ---------------------------------------------------------------------------------
    def release(self, name, namespace):
        history = self.revisions.get(name)
        if not history:
            return None
        last = history[-1]
        return {k: last[k] for k in ("status", "description")} | {"revision": last["revision"]}

    def history(self, name, namespace):
        return [{"revision": r["revision"], "status": r["status"]} for r in self.revisions[name]]

    def record(self, name, status, description, good):
        history = self.revisions.setdefault(name, [])
        for item in history:
            if item["status"] == "deployed":
                item["status"] = "superseded"
        history.append(
            {
                "revision": len(history) + 1,
                "status": status,
                "description": description,
                "good": good,
            }
        )

    def helm(self, *args, timeout=60):
        if args[0] == "rollback":
            name, revision = args[1], int(args[2])
            self.mutations.append(("rollback", name, revision))
            if self.rollback_fails:
                return done(1)
            good = self.revisions[name][revision - 1]["good"]
            self.record(name, "deployed", f"Rollback to {revision}", good)
            self.workloads(name, good)
            return done()
        assert args[:2] == ("upgrade", "--install") and "--wait" not in args
        name, description = args[2], args[args.index("--description") + 1]
        self.mutations.append(("helm", name))
        good = name not in self.broken
        self.record(name, "deployed", description, good)
        self.workloads(name, good)
        return done()

    # --- workloads ----------------------------------------------------------------------------
    def workloads(self, name, good):
        revision = "good" if good else "bad"
        if name == "demo-db":
            self.objects.setdefault(
                ("pvc", "demo-db"),
                {"spec": {"storageClassName": "local-path"}, "status": {"phase": "Bound"}},
            )
            if not good:
                self.pods[name] = {"name": f"{name}-0", "revision": "bad"}
            stuck = name in self.pods  # a Pending Pod of the bad revision blocks the controller
            self.objects[("statefulset", name)] = {
                "metadata": {"generation": 1},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app.kubernetes.io/instance": name}},
                },
                "status": {
                    "observedGeneration": 1,
                    "updateRevision": revision,
                    "readyReplicas": 0 if stuck else 1,
                    "updatedReplicas": 0 if stuck else 1,
                },
            }
        else:
            ready = 1 if good else 0
            self.objects[("deployment", name)] = {
                "metadata": {"generation": 1},
                "spec": {"replicas": 1},
                "status": {"observedGeneration": 1, "readyReplicas": ready, "updatedReplicas": 1},
            }
        self.objects[("endpoints", name)] = {"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}

    def kubectl(self, *args, content=None, timeout=40):
        if args[:2] == ("get", "pods"):
            items = [
                {
                    "metadata": {
                        "name": pod["name"],
                        "labels": {"controller-revision-hash": pod["revision"]},
                        "ownerReferences": [{"kind": "StatefulSet", "name": owner}],
                    },
                    "status": {
                        "phase": "Pending",
                        "conditions": [{"type": "Ready", "status": "False"}],
                    },
                }
                for owner, pod in self.pods.items()
            ]
            return CompletedProcess([], 0, json.dumps({"items": items}).encode(), b"")
        if args[:2] == ("delete", "pod"):
            self.mutations.append(("delete-pod", args[2]))
            owner = args[2].rsplit("-", 1)[0]
            self.pods.pop(owner, None)
            statefulset = self.objects[("statefulset", owner)]
            if statefulset["status"]["updateRevision"] == "good":
                statefulset["status"] |= {"readyReplicas": 1, "updatedReplicas": 1}
            elif owner not in self.pods:  # recreated at the bad revision: still Pending
                self.pods[owner] = {"name": args[2], "revision": "bad"}
            return done()
        return super().kubectl(*args, content=content, timeout=timeout)


def deployed_cluster():
    cluster = HistoryCluster()
    assert run(cluster, revision_job(1)) == ("SUCCEEDED", "READY")
    cluster.mutations.clear()
    return cluster


def statuses(cluster, name):
    return [item["status"] for item in cluster.revisions[name]]


def test_failed_db_revision_rolls_back_and_keeps_the_running_db_and_app():
    """C-02: rev2 never schedules its DB Pod; the DB returns to rev1 and the app is untouched."""
    cluster = deployed_cluster()
    cluster.broken.add("demo-db")
    assert run(cluster, revision_job(2)) == ("FAILED", "WORKLOAD_NOT_READY")
    assert cluster.mutations == [
        ("helm", "demo-db"),
        ("rollback", "demo-db", 1),
        ("delete-pod", "demo-db-0"),
    ]  # the app Release is never upgraded on top of a failed DB
    assert statuses(cluster, "demo-db") == ["superseded", "superseded", "deployed"]
    assert cluster.objects[("statefulset", "demo-db")]["status"]["readyReplicas"] == 1
    assert cluster.revisions["demo-app"][-1]["revision"] == 1


def test_next_revision_applies_after_a_rolled_back_failure():
    cluster = deployed_cluster()
    cluster.broken.add("demo-db")
    run(cluster, revision_job(2))
    cluster.broken.clear()
    assert run(cluster, revision_job(3)) == ("SUCCEEDED", "READY")
    assert statuses(cluster, "demo-db")[-1] == "deployed"


def test_failed_app_revision_restores_only_the_app():
    cluster = deployed_cluster()
    cluster.broken.add("demo-app")
    assert run(cluster, revision_job(2)) == ("FAILED", "WORKLOAD_NOT_READY")
    assert ("rollback", "demo-app", 1) in cluster.mutations
    assert not any(m[:2] == ("rollback", "demo-db") for m in cluster.mutations)
    assert cluster.objects[("deployment", "demo-app")]["status"]["readyReplicas"] == 1


def test_release_left_failed_is_restored_then_reapplied():
    """A Release an older Agent left `failed` (rev1 deployed, rev2 failed): inspect history,
    return to the deployed Revision, clear its stuck Pod, then apply the new Revision."""
    cluster = deployed_cluster()
    cluster.record("demo-db", "failed", "cmp:old", good=False)
    cluster.workloads("demo-db", False)
    cluster.revisions["demo-db"][0]["status"] = "deployed"  # Helm keeps rev1 deployed
    assert run(cluster, revision_job(3)) == ("SUCCEEDED", "READY")
    db = [m for m in cluster.mutations if "demo-db" in m or "demo-db-0" in m]
    assert db[:3] == [("rollback", "demo-db", 1), ("delete-pod", "demo-db-0"), ("helm", "demo-db")]


def test_failed_first_install_is_reapplied_and_its_stuck_pod_cleared():
    """Nothing ran before, so there is nothing to restore; the next Revision re-applies over
    the failed Release and replaces the Pod stuck at the old spec."""
    cluster = HistoryCluster()
    cluster.broken.add("demo-db")
    assert run(cluster, revision_job(1)) == ("FAILED", "WORKLOAD_NOT_READY")
    assert not any(m[0] == "rollback" for m in cluster.mutations)
    cluster.revisions["demo-db"][-1]["status"] = "failed"
    cluster.broken.clear()
    assert run(cluster, revision_job(2)) == ("SUCCEEDED", "READY")
    assert ("delete-pod", "demo-db-0") in cluster.mutations


def test_rollback_failure_requires_inspection():
    cluster = deployed_cluster()
    cluster.broken.add("demo-db")
    cluster.rollback_fails = True
    assert run(cluster, revision_job(2)) == ("UNKNOWN", "ROLLBACK_FAILED")


@pytest.mark.parametrize("status", ["pending-upgrade", "pending-rollback", "uninstalling"])
def test_release_still_in_progress_is_never_touched(status):
    """Timeouts/cancel of an interrupted Helm step belong to ticket 52."""
    cluster = deployed_cluster()
    cluster.revisions["demo-db"][-1]["status"] = status
    assert run(cluster, revision_job(2)) == ("UNKNOWN", "INSPECTION_REQUIRED")
    assert cluster.mutations == []


def test_rolled_back_failure_reports_only_codes():
    cluster = deployed_cluster()
    cluster.broken.add("demo-db")
    client = FakeClient(revision_job(2).model_dump(mode="json"))
    result = RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    [report] = client.reports
    assert (report["status"], report["code"]) == ("FAILED", "WORKLOAD_NOT_READY")
    assert set(report) == {"agent_id", "credential", "job_id", "status", "code"}
    assert result == {"job_id": report["job_id"], "status": "FAILED", "code": "WORKLOAD_NOT_READY"}


def test_rollback_failure_is_classified_at_the_helm_release_check():
    """`cmp_contracts` ProvisioningResult: UNKNOWN at helm_releases → CHECK_STATE_FIRST."""
    checks, stage = _checks("ON_PREM", "WORKLOADS", "UNKNOWN", "ROLLBACK_FAILED", None, {})
    failed = next(check for check in checks if check.code == "ROLLBACK_FAILED")
    assert (failed.name, failed.status, stage) == ("helm_releases", "UNKNOWN", "WORKLOAD_DEPLOY")


@pytest.mark.parametrize(("value", "seconds"), [("90s", 90), ("5m", 300), ("1m30s", 90)])
def test_helm_timeout_durations(value, seconds):
    assert duration_seconds(value) == seconds


@pytest.mark.parametrize("value", ["", "5", "5x", "1.5m", "m5"])
def test_unsupported_durations_are_rejected(value):
    with pytest.raises(ValueError):
        duration_seconds(value)


def test_history_keeps_only_revision_numbers_and_statuses(tmp_path, monkeypatch):
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("{}")
    target = ClusterTarget(kubeconfig, "k3d-cmp-test", "cluster-a")
    output = [{"revision": 1, "status": "deployed", "description": "x", "chart": "c"}]
    calls = []

    def fake_exec(argv, content, timeout):
        calls.append(argv)
        return CompletedProcess(argv, 0, json.dumps(output).encode(), b"")

    monkeypatch.setattr(target, "_exec", fake_exec)
    assert target.history("demo-db", "demo") == [{"revision": 1, "status": "deployed"}]
    assert calls[0][:5] == [
        "helm",
        "--kubeconfig",
        str(kubeconfig),
        "--kube-context",
        "k3d-cmp-test",
    ]
