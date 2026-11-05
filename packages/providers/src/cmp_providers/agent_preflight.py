"""Agent side of the `NETWORK_PREFLIGHT` job (MNP-01 decision N3).

The Agent runs the same `KubernetesProbeExecutor` as the direct Worker path, with its in-cluster
kubeconfig, and returns only the `ProbeObservation`. Before creating any Pod it checks that the
job targets this Agent and cluster and that the probe Namespace is Agent-owned
(`cmp.dev/environment-id`, as for rollout and tunnel forwards). The probe image comes from the
Agent's own configuration (digest-pinned, allowlisted); the job cannot choose it.
"""

from collections.abc import Callable

from cmp_contracts.network_preflight import NetworkPreflightJob

from cmp_providers.network_preflight import ProbeExecutor, ProbeLocationUnavailable
from cmp_providers.onprem_agent import ENVIRONMENT, ClusterUnavailable

PROBED = "PROBED"


class PreflightJobRunner:
    """`run(job) -> (status, code, observation | None)`; never raises for cluster errors."""

    def __init__(self, target, executor_factory: Callable[[], ProbeExecutor] | None):
        self.target, self.executor_factory = target, executor_factory

    def run(self, job: NetworkPreflightJob) -> tuple[str, str, dict | None]:
        if self.executor_factory is None:
            return "FAILED", "PREFLIGHT_NOT_CONFIGURED", None
        if job.cluster_uid != self.target.cluster_uid:
            return "FAILED", "CLUSTER_MISMATCH", None
        try:
            namespace = self.target.get("namespace", job.request.probe_target.namespace)
        except ClusterUnavailable:
            return "UNKNOWN", "PROBE_LOCATION_UNAVAILABLE", None
        annotations = ((namespace or {}).get("metadata") or {}).get("annotations") or {}
        if not annotations.get(ENVIRONMENT):
            return "FAILED", "NAMESPACE_NOT_OWNED", None
        try:
            executor = self.executor_factory()
        except ValueError:  # image not digest-pinned / not allowlisted
            return "FAILED", "PREFLIGHT_NOT_CONFIGURED", None
        try:
            observation = executor.probe(job.request)
        except ProbeLocationUnavailable as exc:
            return "UNKNOWN", exc.code, None
        return "SUCCEEDED", PROBED, observation.model_dump(mode="json")


def probe_executor_factory(target, image: str | None, allowed_images: frozenset[str]):
    """Agent wiring: None when no probe image is configured (jobs then fail with a code)."""
    if not image:
        return None
    from cmp_providers.network_preflight_k8s import KubernetesProbeExecutor

    def factory():
        return KubernetesProbeExecutor(target.kubectl, image=image, allowed_images=allowed_images)

    return factory
