"""Keep the running Revision when an ON_PREM Helm upgrade does not settle (ticket 51, C-02).

The Agent upgrades without `helm --wait` and waits here itself, because a single-replica
StatefulSet whose new Pod never becomes Ready (unschedulable, wrong architecture) blocks its
controller: even after the spec is rolled back, the controller waits for that Pod forever (k3d,
2026-11-06). While waiting, a Pod that is not Ready and belongs to an older revision than the
StatefulSet's `updateRevision` is deleted so the controller can replace it; a Ready Pod is never
touched, and the PVC (data) is never touched.

`target` is `onprem_agent.ClusterTarget` (or a test double): `get`, `kubectl`, `helm`, `history`,
`now`, `pause`, `helm_timeout`. Functions return booleans; error codes stay in `onprem_agent`.
"""

import json
import re

POLL_SECONDS = 2
REVISION_LABEL = "controller-revision-hash"
_DURATION = re.compile(r"(\d+)([hms])")
_UNIT = {"h": 3600, "m": 60, "s": 1}


def duration_seconds(value: str) -> int:
    """Helm/Go duration subset used by `--helm-timeout` (`90s`, `5m`, `1m30s`)."""
    parts = _DURATION.findall(value or "")
    if not parts or "".join(n + u for n, u in parts) != value:
        raise ValueError("unsupported duration")
    return sum(int(number) * _UNIT[unit] for number, unit in parts)


def workload_settled(resource: dict) -> bool:
    """All desired replicas are updated and Ready, with no surplus or unavailable Pod."""
    status, desired = resource.get("status", {}), resource["spec"].get("replicas", 1)
    return (
        desired >= 1
        and status.get("observedGeneration", 0) >= resource["metadata"]["generation"]
        and status.get("readyReplicas", 0) == desired
        and status.get("updatedReplicas", 0) == desired
        and status.get("replicas", desired) == desired
        and status.get("unavailableReplicas", 0) == 0
    )


def _ready(pod: dict) -> bool:
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def _stuck_pods(target, statefulset: dict, name: str, namespace: str) -> list[str]:
    status = statefulset.get("status", {})
    update = status.get("updateRevision")
    if not update or status.get("observedGeneration", 0) < statefulset["metadata"]["generation"]:
        return []  # the controller has not computed the target revision yet
    labels = ((statefulset.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    if not labels:
        return []
    selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    listed = target.kubectl("get", "pods", "-n", namespace, "-l", selector, "-o", "json")
    if listed.returncode:
        return []
    stuck = []
    for pod in json.loads(listed.stdout or b"{}").get("items", []):
        meta = pod.get("metadata") or {}
        owned = any(
            ref.get("kind") == "StatefulSet" and ref.get("name") == name
            for ref in meta.get("ownerReferences") or []
        )
        revision = (meta.get("labels") or {}).get(REVISION_LABEL)
        if (
            owned
            and not meta.get("deletionTimestamp")
            and revision
            and revision != update
            and not _ready(pod)
        ):
            stuck.append(meta["name"])
    return stuck


def settle(target, kind: str, name: str, namespace: str) -> bool:
    """Wait up to `helm_timeout` for the workload to settle, unblocking a stuck StatefulSet."""
    deadline = target.now() + duration_seconds(target.helm_timeout)
    while True:
        resource = target.get(kind, name, namespace)
        if resource is not None:
            if workload_settled(resource):
                return True
            if kind == "statefulset":
                for pod in _stuck_pods(target, resource, name, namespace):
                    target.kubectl("delete", "pod", pod, "-n", namespace, "--wait=false")
        if target.now() >= deadline:
            return False
        target.pause(POLL_SECONDS)


def last_deployed_revision(target, release: str, namespace: str) -> int | None:
    """Newest `deployed` revision in the Release history (Helm keeps it after a failed upgrade)."""
    deployed = [
        int(item["revision"])
        for item in target.history(release, namespace)
        if item.get("status") == "deployed"
    ]
    return max(deployed) if deployed else None


def restore(target, kind: str, release: str, namespace: str, revision: int) -> bool:
    """Roll the Release back to `revision` (approved and deployed before) and wait for it."""
    result = target.helm("rollback", release, str(revision), "-n", namespace, timeout=300)
    return result.returncode == 0 and settle(target, kind, release, namespace)
