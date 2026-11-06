"""Ticket 52 (#187) Agent side: environment teardown, start deadline, unsupported job types and
the pull Secret owner after a failed rollout (C-14). In-memory cluster double; the real Helm/K3s
behavior is checked on isolated k3d (scripts/smoke_onprem_rollout.py steps 7–9)."""

import json
from subprocess import CompletedProcess

import pytest
from cmp_contracts.onprem_teardown import TEARDOWN_MODE, TeardownJob, teardown_job_id
from cmp_providers.agent_image_pull import JOB_ID, MANAGED, PULL_PURPOSE, PURPOSE
from cmp_providers.agent_teardown import TeardownRunner
from cmp_providers.onprem_agent import ENVIRONMENT, ClusterUnavailable, RolloutAgent
from test_onprem_agent import FakeClient, FakeCluster, job

SOURCE = "rollout-0123456789abcdef01234567"


def teardown(**override):
    payload = {
        "mode": TEARDOWN_MODE,
        "job_id": teardown_job_id(SOURCE, False),
        "source_job_id": SOURCE,
        "environment_id": "onprem-prod-001",
        "agent_id": "agent-a",
        "cluster_uid": "cluster-a",
        "namespace": "demo",
        "releases": ["demo-app", "demo-db"],
        "database_release": "demo-db",
        "database_secret": "demo-db-auth",
        "delete_data": False,
    }
    payload.update(override)
    return TeardownJob.model_validate(payload)


def ok(stdout=b""):
    return CompletedProcess([], 0, stdout, b"")


def failed():
    return CompletedProcess([], 1, b"", b"error")


class Namespace:
    """What lives in the target Namespace, as kubectl/helm would see and change it."""

    def __init__(self):
        self.cluster_uid, self.helm_timeout, self.clock = "cluster-a", "1m", 0.0
        self.available = True
        self.namespace = {"metadata": {"annotations": {ENVIRONMENT: "onprem-prod-001"}}}
        self.releases = {"demo-app", "demo-db"}
        managed = {"labels": {MANAGED: "true"}}
        self.secrets = {
            "demo-db-auth": managed,
            "demo-db-url": managed,
            "demo-app-env-rendered": managed,
            "cmp-ecr-pull": {"labels": {MANAGED: "true", PURPOSE: PULL_PURPOSE}},
            "demo-app-env": {"labels": {}},  # the user's own Secret
        }
        self.policies = {"cmp-default-deny": True, "user-policy": False}  # name -> managed
        self.pvcs = {"demo-db"}
        self.fail = set()  # e.g. {"uninstall demo-db"}
        self.namespace_lingers = False
        self.calls = []

    def now(self):
        return self.clock

    def pause(self, seconds):
        self.clock += seconds

    def get(self, kind, name, namespace=None):
        if not self.available:
            raise ClusterUnavailable()
        if (kind, name) == ("namespace", "kube-system"):
            return {"metadata": {"uid": "cluster-a"}}
        if (kind, name) == ("namespace", "demo"):
            return self.namespace
        return None

    def helm(self, *args, timeout=60):
        self.calls.append(("helm",) + args[:2])
        assert args[0] == "uninstall" and "--ignore-not-found" in args and "--wait" not in args
        if f"uninstall {args[1]}" in self.fail:
            return failed()
        self.releases.discard(args[1])
        return ok()

    def kubectl(self, *args, content=None, timeout=40):
        self.calls.append(("kubectl",) + args)
        if not self.available:
            raise ClusterUnavailable()
        verb, kind = args[0], args[1]
        if verb == "get" and kind == "secrets":
            items = [
                {"metadata": {"name": n, "labels": meta["labels"]}}
                for n, meta in self.secrets.items()
                if meta["labels"].get(MANAGED) == "true"
            ]
            return ok(json.dumps({"items": items}).encode())
        if verb == "delete" and kind == "secret":
            if "delete secret" in self.fail:
                return failed()
            for name in args[2 : args.index("-n")]:
                self.secrets.pop(name, None)
            return ok()
        if verb == "delete" and kind == "networkpolicy":
            assert f"{MANAGED}=true" in args
            self.policies = {n: m for n, m in self.policies.items() if not m}
            return ok()
        if verb == "delete" and kind == "pvc":
            self.pvcs.discard(args[2])
            return ok()
        if verb == "delete" and kind == "namespace":
            if not self.namespace_lingers:
                self.namespace = None
            return ok()
        raise AssertionError(args)


def run(cluster, job=None, deadline=None):
    return TeardownRunner(cluster).run(job or teardown(), deadline=deadline)


def test_keep_data_removes_releases_and_platform_objects_but_keeps_the_data():
    cluster = Namespace()
    assert run(cluster) == ("SUCCEEDED", "TORN_DOWN")
    assert cluster.releases == set()
    assert [c for c in cluster.calls if c[0] == "helm"] == [
        ("helm", "uninstall", "demo-app"),
        ("helm", "uninstall", "demo-db"),
    ]
    # DB password Secret stays with the PVC; the user's Secret and policy are never touched.
    assert set(cluster.secrets) == {"demo-db-auth", "demo-app-env"}
    assert cluster.policies == {"user-policy": False}
    assert cluster.pvcs == {"demo-db"} and cluster.namespace is not None


def test_delete_data_also_removes_the_pvc_and_the_namespace():
    cluster = Namespace()
    assert run(cluster, teardown(delete_data=True)) == ("SUCCEEDED", "TORN_DOWN")
    assert cluster.pvcs == set() and cluster.namespace is None
    assert "demo-db-auth" not in cluster.secrets


def test_namespace_that_does_not_go_away_is_reported_for_inspection():
    cluster = Namespace()
    cluster.namespace_lingers = True
    assert run(cluster, teardown(delete_data=True)) == ("UNKNOWN", "NAMESPACE_DELETE_PENDING")
    assert cluster.clock >= 60  # waited for the Agent's Helm timeout, on the fake clock


def test_foreign_or_missing_namespace():
    cluster = Namespace()
    cluster.namespace = {"metadata": {"annotations": {ENVIRONMENT: "someone-else"}}}
    assert run(cluster) == ("FAILED", "NAMESPACE_NOT_OWNED")
    assert cluster.calls == []
    cluster.namespace = None  # already gone: nothing to do
    assert run(cluster) == ("SUCCEEDED", "TORN_DOWN") and cluster.calls == []


def test_wrong_cluster_and_expired_start_change_nothing():
    cluster = Namespace()
    assert run(cluster, teardown(cluster_uid="cluster-b")) == ("FAILED", "CLUSTER_MISMATCH")
    assert run(cluster, deadline=0.0) == ("FAILED", "JOB_EXPIRED")
    assert cluster.calls == []


def test_any_failed_delete_is_incomplete_and_a_retry_finishes_it():
    cluster = Namespace()
    cluster.fail = {"uninstall demo-db"}
    assert run(cluster) == ("UNKNOWN", "TEARDOWN_INCOMPLETE")
    assert set(cluster.secrets) == {"demo-db-auth", "demo-app-env"}  # the rest still ran
    cluster.fail = set()
    assert run(cluster) == ("SUCCEEDED", "TORN_DOWN") and cluster.releases == set()


def test_unreachable_cluster_raises_so_the_job_stays_running():
    cluster = Namespace()
    cluster.available = False
    with pytest.raises(ClusterUnavailable):
        run(cluster)


# --- Agent dispatch ---------------------------------------------------------------------


class Client(FakeClient):
    def __init__(self, payload, expires=None):
        super().__init__(payload)
        self.expires = expires

    def poll_once(self, *, agent_id, credential):
        claimed = super().poll_once(agent_id=agent_id, credential=credential)
        return claimed | ({"expires_in_seconds": self.expires} if self.expires is not None else {})


def test_agent_runs_a_teardown_job_and_reports_codes_only():
    cluster = Namespace()
    client = Client(teardown().model_dump(mode="json"), expires=600)
    outcome = RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    assert outcome == {"job_id": teardown().job_id, "status": "SUCCEEDED", "code": "TORN_DOWN"}
    [report] = client.reports
    assert (report["status"], report["code"]) == ("SUCCEEDED", "TORN_DOWN")


def test_teardown_for_another_agent_is_refused():
    client = Client(teardown(agent_id="agent-b").model_dump(mode="json"))
    cluster = Namespace()
    RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    assert client.reports[0]["code"] == "JOB_TARGET_MISMATCH" and cluster.calls == []


def test_unknown_job_type_is_refused_without_any_change():
    """D11: what an Agent answers for a job type it does not know (older Agents included)."""
    client = Client({"mode": "SOMETHING_NEW", "job_id": "job-x"})
    client.payload["job_id"] = "job-x"
    cluster = FakeCluster()
    RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    assert (client.reports[0]["status"], client.reports[0]["code"]) == ("FAILED", "UNSUPPORTED_JOB")
    assert cluster.mutations == []


def test_rollout_claimed_with_no_time_left_changes_nothing():
    cluster = FakeCluster()
    client = Client(job().model_dump(mode="json"), expires=0)
    outcome = RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    assert (outcome["status"], outcome["code"]) == ("FAILED", "JOB_EXPIRED")
    assert cluster.mutations == []


def test_rollout_within_its_deadline_runs_as_before():
    cluster = FakeCluster()
    client = Client(job().model_dump(mode="json"), expires=600)
    assert RolloutAgent("agent-a", "cred", client, cluster).poll_once()["code"] == "READY"


# --- C-14: pull Secret owner after a failed rollout ----------------------------------------


def test_failed_rollout_hands_the_pull_secret_back_to_the_deployed_job():
    from cmp_providers.onprem_agent import RolloutRunner
    from test_image_pull import PULL, PullCluster, pull_job, pull_secret
    from test_onprem_agent import fetch

    class Annotating(PullCluster):
        def history(self, name, namespace):
            return []  # no deployed revision kept by this double

        def kubectl(self, *args, content=None, timeout=40):
            if args[:2] == ("annotate", "secret"):
                self.mutations.append(("annotate", args[2]))
                key, value = args[-2].split("=", 1)
                self.objects[("secret", args[2])]["metadata"]["annotations"][key] = value
                return ok()
            return super().kubectl(*args, content=content, timeout=timeout)

    def owner(cluster):
        return pull_secret(cluster)[0]["metadata"]["annotations"][JOB_ID]

    cluster = Annotating()
    first = pull_job()
    assert RolloutRunner(cluster, fetch, None, lambda: PULL).rollout(first)[1] == "READY"
    second = pull_job(job_id="rollout-ffffffffffffffffffffffff", revision=2)
    cluster.broken = {"demo-app"}
    status, _ = RolloutRunner(cluster, fetch, None, lambda: PULL).rollout(second)
    assert status != "SUCCEEDED" and owner(cluster) == first.job_id
    # A successful rollout keeps its own job as the owner.
    cluster.broken = set()
    third = pull_job(job_id="rollout-eeeeeeeeeeeeeeeeeeeeeeee", revision=3)
    assert RolloutRunner(cluster, fetch, None, lambda: PULL).rollout(third)[1] == "READY"
    assert owner(cluster) == third.job_id
