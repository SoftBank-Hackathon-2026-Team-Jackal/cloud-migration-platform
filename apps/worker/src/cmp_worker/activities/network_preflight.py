"""Activity `run_network_preflight` (MNP-006, decisions N1·N3) on the Data Migration activity queue.

Data Migration calls it once before the initial copy/CDC and passes the result to the pure gate
`cmp_providers.network_preflight.may_start_data_migration` (deterministic, safe in a Workflow).
The probe runs where replication runs:

- `probe_target.agent_id` is a registered on-premises Agent (ON_PREM): queue a
  `NETWORK_PREFLIGHT` Agent job and wait for its observation (the Agent probes from inside the
  laptop K3s, which opens no inbound port).
- Otherwise: the Worker's explicit kubeconfig/context (`PREFLIGHT_KUBECONFIG`, EKS/GKE or tests).

Either way the verdict comes from the same `run_network_preflight`, the result is emitted as
`data.preflight` and returned. Nothing here can turn a missing location into PASS.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Protocol

from cmp_contracts.network_preflight import NetworkPreflightRequest, ProbeObservation
from cmp_providers.network_preflight import (
    ProbeLocationUnavailable,
    input_fingerprint,
    may_start_data_migration,
    run_network_preflight,
)
from pydantic import ValidationError
from temporalio import activity

HEARTBEAT_SECONDS = 20
# Probe Pod wait (timeout + 90 s) + route Pod (60 s) + Agent idle poll + margin.
AGENT_EXTRA_WAIT_SECONDS = 240
# Older Agents (before this job type) reject it; say so instead of a generic failure.
AGENT_CODES = {"UNSUPPORTED_JOB": "AGENT_JOB_UNSUPPORTED"}


class _Unavailable:
    def __init__(self, code: str = "PROBE_LOCATION_UNAVAILABLE"):
        self.code = code

    def probe(self, request):
        raise ProbeLocationUnavailable(self.code)


class AgentPreflightTransport(Protocol):
    def is_agent(self, agent_id: str) -> bool: ...
    def submit(self, request: NetworkPreflightRequest, fingerprint: str) -> str: ...
    def state(self, job_id: str) -> tuple[str, str | None, dict | None]: ...


class AgentJobExecutor:
    """`ProbeExecutor` that delegates the probe to the on-prem Agent through the job queue.
    Runs in a worker thread; the Activity heartbeats meanwhile."""

    def __init__(
        self,
        transport: AgentPreflightTransport,
        *,
        poll_seconds: float = 2.0,
        wait_seconds: float | None = None,
    ):
        """`wait_seconds` default: request timeout + AGENT_EXTRA_WAIT_SECONDS."""
        self.transport, self.poll, self.wait = transport, poll_seconds, wait_seconds
        self.job_id: str | None = None

    def probe(self, request: NetworkPreflightRequest) -> ProbeObservation:
        try:
            self.job_id = self.transport.submit(request, input_fingerprint(request))
        except ProbeLocationUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - registry/DB down: never PASS
            raise ProbeLocationUnavailable(getattr(exc, "code", "AGENT_REGISTRY_UNAVAILABLE"))
        wait = (
            self.wait
            if self.wait is not None
            else request.timeout_seconds + AGENT_EXTRA_WAIT_SECONDS
        )
        deadline = time.monotonic() + wait
        while True:
            try:
                status, code, observation = self.transport.state(self.job_id)
            except Exception:  # noqa: BLE001
                raise ProbeLocationUnavailable("AGENT_REGISTRY_UNAVAILABLE") from None
            if status == "SUCCEEDED":
                try:
                    return ProbeObservation.model_validate(observation)
                except (ValidationError, ValueError, TypeError):
                    raise ProbeLocationUnavailable("AGENT_RESULT_INVALID") from None
            if status in ("FAILED", "UNKNOWN"):
                code = code or "AGENT_RESULT_MISSING"
                raise ProbeLocationUnavailable(AGENT_CODES.get(code, code))
            if status == "MISSING":
                raise ProbeLocationUnavailable("AGENT_RESULT_MISSING")
            if time.monotonic() >= deadline:
                # The job stays queued/running; a retry of the same request reuses it.
                code = "AGENT_OFFLINE" if status == "QUEUED" else "AGENT_RESULT_PENDING"
                raise ProbeLocationUnavailable(code)
            time.sleep(self.poll)


class NetworkPreflightActivities:
    def __init__(self, executor_factory):
        """`executor_factory(request) -> ProbeExecutor` chooses the Agent or direct path."""
        self._executor_factory = executor_factory

    def _run(self, parsed: NetworkPreflightRequest):
        return run_network_preflight(parsed, self._executor_factory(parsed))

    @activity.defn(name="run_network_preflight")
    async def run_network_preflight(self, request: dict) -> dict:
        parsed = NetworkPreflightRequest.model_validate(request)
        work = asyncio.create_task(asyncio.to_thread(self._run, parsed))
        while not work.done():
            await asyncio.wait({work}, timeout=HEARTBEAT_SECONDS)
            activity.heartbeat("probing")
        result = work.result()
        # Imported here: the events module pulls in API settings, which the Workflow sandbox
        # (it re-imports modules that register this Activity) must not load.
        from cmp_worker.preflight_events import emit_preflight

        may_start, _ = may_start_data_migration(result, parsed)
        emit_preflight(parsed, result, may_start)
        return result.model_dump(mode="json")


class DbAgentPreflightTransport:
    """Business-DB job queue shared with the platform API (same as `execute_on_prem_plan`)."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def is_agent(self, agent_id: str) -> bool:
        from cmp_api.agent_preflight import registered_agent

        with self._session_factory() as session:
            return registered_agent(session, agent_id) is not None

    def submit(self, request: NetworkPreflightRequest, fingerprint: str) -> str:
        from cmp_api.agent_preflight import PreflightJobError, submit_preflight

        with self._session_factory() as session:
            try:
                return submit_preflight(session, request, fingerprint)
            except PreflightJobError as exc:
                raise ProbeLocationUnavailable(exc.code) from None

    def state(self, job_id: str):
        from cmp_api.agent_preflight import preflight_state

        with self._session_factory() as session:
            return preflight_state(session, job_id)


def choose_executor(request: NetworkPreflightRequest, transport, direct, **agent_options):
    """ON_PREM Agent path when `probe_target.agent_id` is a registered Agent (registered Agents
    are on-premises by definition); otherwise the direct kubeconfig path. A registry failure is
    UNKNOWN, never a silent switch to another probe location."""
    if transport is not None:
        try:
            on_prem = transport.is_agent(request.probe_target.agent_id)
        except Exception:  # noqa: BLE001
            return _Unavailable("AGENT_REGISTRY_UNAVAILABLE")
        if on_prem:
            return AgentJobExecutor(transport, **agent_options)
    return direct()


def _direct_executor():
    kubeconfig = os.environ.get("PREFLIGHT_KUBECONFIG")
    context = os.environ.get("PREFLIGHT_CONTEXT")
    image = os.environ.get("PREFLIGHT_PROBE_IMAGE", "")
    allowed = frozenset(filter(None, os.environ.get("PREFLIGHT_ALLOWED_IMAGES", "").split(",")))
    if not (kubeconfig and context and Path(kubeconfig).is_file()):
        return _Unavailable()
    from cmp_providers.network_preflight_k8s import KubernetesProbeExecutor
    from cmp_providers.onprem_agent import ClusterTarget

    target = ClusterTarget(Path(kubeconfig), context, "network-preflight")
    try:
        return KubernetesProbeExecutor(target.kubectl, image=image, allowed_images=allowed)
    except ValueError:
        return _Unavailable()


def build_network_preflight_activities(transport=None) -> NetworkPreflightActivities:
    """Worker wiring. Unset settings give UNKNOWN/PROBE_LOCATION_UNAVAILABLE, never PASS.
    `transport=None` uses the business DB (`cmp_api.db.SessionLocal`)."""
    if transport is None:
        from cmp_api.db import SessionLocal

        transport = DbAgentPreflightTransport(SessionLocal)
    return NetworkPreflightActivities(
        lambda request: choose_executor(request, transport, _direct_executor)
    )
