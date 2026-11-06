"""`ENVIRONMENT_TEARDOWN` Agent job (ticket 52, decisions D8/D9).

Removes what an earlier rollout deployed in one Agent-owned Namespace: the Helm Releases
(application first), the platform's managed Secrets and NetworkPolicies. The database PVC and its
password Secret stay unless `delete_data`, which also deletes the PVC and the Namespace. Every
step tolerates "already gone", so a retry after a partial run finishes the job. The Namespace is
touched only when its `cmp.dev/environment-id` annotation is the job's (as for rollouts).

`target` is `onprem_agent.ClusterTarget` (or a test double): `get`, `kubectl`, `helm`, `now`,
`pause`, `helm_timeout`, `cluster_uid`. Codes only; kubectl/helm output is never reported.
"""

import json

from cmp_contracts.onprem_teardown import TeardownJob

from cmp_providers.agent_rollout_recovery import POLL_SECONDS, duration_seconds

MANAGED = "cmp.dev/managed"
ENVIRONMENT = "cmp.dev/environment-id"


class TeardownRunner:
    def __init__(self, target):
        self.target = target

    def run(self, job: TeardownJob, *, deadline: float | None = None) -> tuple[str, str]:
        """(status, code). Raises `ClusterUnavailable` (job stays RUNNING, retried later)."""
        system = self.target.get("namespace", "kube-system")
        uid = ((system or {}).get("metadata") or {}).get("uid")
        if job.cluster_uid != self.target.cluster_uid or uid != job.cluster_uid:
            return "FAILED", "CLUSTER_MISMATCH"
        namespace = self.target.get("namespace", job.namespace)
        if namespace is None:
            return "SUCCEEDED", "TORN_DOWN"  # nothing left to remove
        annotations = (namespace.get("metadata") or {}).get("annotations") or {}
        if annotations.get(ENVIRONMENT) != job.environment_id:
            return "FAILED", "NAMESPACE_NOT_OWNED"
        if deadline is not None and self.target.now() >= deadline:
            return "FAILED", "JOB_EXPIRED"  # D4: never start removing after the deadline

        complete = True
        for release in job.releases:  # any Helm status, including pending-*/uninstalling
            result = self.target.helm(
                "uninstall", release, "-n", job.namespace, "--ignore-not-found", timeout=300
            )
            complete &= result.returncode == 0
        complete &= self._delete_secrets(job)
        policies = self.target.kubectl(
            "delete", "networkpolicy", "-n", job.namespace, "-l", f"{MANAGED}=true",
            "--ignore-not-found",
        )  # fmt: skip
        complete &= policies.returncode == 0
        if job.delete_data:
            pvc = self.target.kubectl(
                "delete", "pvc", job.database_release, "-n", job.namespace,
                "--ignore-not-found", "--wait=false",
            )  # fmt: skip
            complete &= pvc.returncode == 0
            if complete:
                return self._delete_namespace(job)
        return ("SUCCEEDED", "TORN_DOWN") if complete else ("UNKNOWN", "TEARDOWN_INCOMPLETE")

    def _delete_secrets(self, job: TeardownJob) -> bool:
        listed = self.target.kubectl(
            "get", "secrets", "-n", job.namespace, "-l", f"{MANAGED}=true", "-o", "json"
        )
        if listed.returncode:
            return False
        names = sorted(
            item["metadata"]["name"]
            for item in json.loads(listed.stdout or b"{}").get("items", [])
            if job.delete_data or item["metadata"]["name"] != job.database_secret
        )
        if not names:
            return True
        result = self.target.kubectl(
            "delete", "secret", *names, "-n", job.namespace, "--ignore-not-found"
        )
        return result.returncode == 0

    def _delete_namespace(self, job: TeardownJob) -> tuple[str, str]:
        result = self.target.kubectl(
            "delete", "namespace", job.namespace, "--ignore-not-found", "--wait=false"
        )
        if result.returncode:
            return "UNKNOWN", "TEARDOWN_INCOMPLETE"
        limit = self.target.now() + duration_seconds(self.target.helm_timeout)
        while self.target.get("namespace", job.namespace) is not None:
            if self.target.now() >= limit:
                return "UNKNOWN", "NAMESPACE_DELETE_PENDING"
            self.target.pause(POLL_SECONDS)
        return "SUCCEEDED", "TORN_DOWN"
