"""Activity `run_network_preflight` (MNP-006, decision N1) on the Data Migration activity queue.

Data Migration calls it once before the initial copy/CDC and passes the result to the pure gate
`cmp_providers.network_preflight.may_start_data_migration` (deterministic, safe in a Workflow).
The probe runs where replication runs; the Worker only needs an explicit kubeconfig/context for
that cluster (the Agent-job path follows Provisioning 15, decision N3).
"""

import asyncio
import os
from pathlib import Path

from cmp_contracts.network_preflight import NetworkPreflightRequest
from cmp_providers.network_preflight import ProbeLocationUnavailable, run_network_preflight
from temporalio import activity

HEARTBEAT_SECONDS = 20


class _Unconfigured:
    def probe(self, request):
        raise ProbeLocationUnavailable()


class NetworkPreflightActivities:
    def __init__(self, executor_factory):
        self._executor_factory = executor_factory

    @activity.defn(name="run_network_preflight")
    async def run_network_preflight(self, request: dict) -> dict:
        parsed = NetworkPreflightRequest.model_validate(request)
        work = asyncio.create_task(
            asyncio.to_thread(run_network_preflight, parsed, self._executor_factory())
        )
        while not work.done():
            await asyncio.wait({work}, timeout=HEARTBEAT_SECONDS)
            activity.heartbeat("probing")
        return work.result().model_dump(mode="json")


def build_network_preflight_activities() -> NetworkPreflightActivities:
    """Worker wiring. Unset settings give UNKNOWN/PROBE_LOCATION_UNAVAILABLE, never PASS."""

    def factory():
        kubeconfig = os.environ.get("PREFLIGHT_KUBECONFIG")
        context = os.environ.get("PREFLIGHT_CONTEXT")
        image = os.environ.get("PREFLIGHT_PROBE_IMAGE", "")
        allowed = frozenset(filter(None, os.environ.get("PREFLIGHT_ALLOWED_IMAGES", "").split(",")))
        if not (kubeconfig and context and Path(kubeconfig).is_file()):
            return _Unconfigured()
        from cmp_providers.network_preflight_k8s import KubernetesProbeExecutor
        from cmp_providers.onprem_agent import ClusterTarget

        target = ClusterTarget(Path(kubeconfig), context, "network-preflight")
        try:
            return KubernetesProbeExecutor(target.kubectl, image=image, allowed_images=allowed)
        except ValueError:
            return _Unconfigured()

    return NetworkPreflightActivities(factory)
