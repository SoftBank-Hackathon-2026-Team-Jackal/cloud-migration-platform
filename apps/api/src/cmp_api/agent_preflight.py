"""Network Preflight Agent jobs (MNP-01 decision N3) on the owner-scoped Agent transport.

The Worker queues one `NETWORK_PREFLIGHT` job per (migration, input fingerprint) for the Agent
named in `probe_target`; the Agent runs the same `KubernetesProbeExecutor` in its cluster and
reports only the `ProbeObservation` (codes, IPs, counters). The verdict is computed on the
Worker by the shared pure `run_network_preflight`, so an Agent never decides PASS by itself.

- Duplicate requests reuse the QUEUED/RUNNING job. A terminal observation is returned again for
  `REUSE_SECONDS` (lost Activity response / retry); after that the same job is queued again so
  a fixed Secret value or route is re-probed instead of returning a stale FAIL.
- Only the Agent whose `agent_id` and registered `cluster_uid` match `probe_target` gets the job.
"""

import json
from datetime import UTC, datetime

from cmp_contracts.network_preflight import (
    PREFLIGHT_JOB_MODE,
    NetworkPreflightJob,
    NetworkPreflightRequest,
    ProbeObservation,
    preflight_job_id,
)
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cmp_api.agent_registry import AgentJob, AgentRegistrationError, RegisteredAgent

TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN"}
REUSE_SECONDS = 300
PROBED = "PROBED"
MAX_OBSERVATION_BYTES = 4096


class PreflightJobError(ValueError):
    """Code-only error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _epoch(now: datetime | None) -> int:
    return int((now or datetime.now(UTC)).timestamp())


def is_preflight(payload) -> bool:
    return isinstance(payload, dict) and payload.get("mode") == PREFLIGHT_JOB_MODE


def registered_agent(session: Session, agent_id: str) -> RegisteredAgent | None:
    return session.get(RegisteredAgent, agent_id)


def submit_preflight(
    session: Session,
    request: NetworkPreflightRequest,
    fingerprint: str,
    *,
    reuse_seconds: int = REUSE_SECONDS,
    now: datetime | None = None,
) -> str:
    """Queue (or reuse) the job for this exact request and return its ID."""
    target = request.probe_target
    agent = session.get(RegisteredAgent, target.agent_id)
    if agent is None:
        raise PreflightJobError("AGENT_NOT_REGISTERED")
    if agent.revoked_at is not None:
        raise PreflightJobError("AGENT_REVOKED")
    if agent.cluster_uid != target.cluster_uid:
        raise PreflightJobError("AGENT_TARGET_MISMATCH")
    job_id = preflight_job_id(request.migration_id, fingerprint)
    payload = NetworkPreflightJob(
        mode=PREFLIGHT_JOB_MODE,
        job_id=job_id,
        agent_id=agent.id,
        cluster_uid=agent.cluster_uid,
        request=request,
    ).model_dump(mode="json")
    job = session.get(AgentJob, job_id)
    if job is None:
        session.add(
            AgentJob(
                id=job_id,
                owner_id=agent.owner_id,
                agent_id=agent.id,
                payload=payload,
                status="QUEUED",
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()  # a concurrent submit queued the same job
        job = session.get(AgentJob, job_id)
    if job.payload != payload or job.agent_id != agent.id or job.owner_id != agent.owner_id:
        raise PreflightJobError("AGENT_JOB_CONFLICT")
    if job.status in TERMINAL:
        observed = (job.result or {}).get("observed_at")
        fresh = isinstance(observed, int) and _epoch(now) - observed <= reuse_seconds
        if not fresh:
            job.status, job.result = "QUEUED", None
            session.commit()
    return job_id


def preflight_state(session: Session, job_id: str) -> tuple[str, str | None, dict | None]:
    """(Agent job status, result code, observation dict) for the Worker to poll."""
    job = session.get(AgentJob, job_id)
    if job is None:
        return "MISSING", None, None
    result = job.result or {}
    return job.status, result.get("code"), result.get("observation")


def claim_block(session: Session, job: AgentJob) -> str | None:
    """Claim-time gate: the job's target must still be this registered Agent and cluster."""
    try:
        parsed = NetworkPreflightJob.model_validate(job.payload)
    except (ValidationError, ValueError):
        return "INVALID_PREFLIGHT_JOB"
    agent = session.get(RegisteredAgent, job.agent_id)
    if (
        agent is None
        or agent.revoked_at is not None
        or parsed.agent_id != agent.id
        or parsed.cluster_uid != agent.cluster_uid
    ):
        return "AGENT_TARGET_MISMATCH"
    return None


def normalise_report(status: str, result: dict, now: datetime | None = None) -> dict:
    """Stored result for a preflight job. SUCCEEDED carries exactly one observation (code
    PROBED); FAILED/UNKNOWN carry a code only (older Agents report UNSUPPORTED_JOB this way)."""
    code = result.get("code")
    observation = result.get("observation")
    if set(result) - {"code", "observation"}:
        raise AgentRegistrationError("INVALID_JOB_RESULT")
    if status == "SUCCEEDED":
        if code != PROBED or observation is None:
            raise AgentRegistrationError("INVALID_JOB_RESULT")
        try:
            parsed = ProbeObservation.model_validate(observation).model_dump(mode="json")
        except (ValidationError, ValueError, TypeError):
            raise AgentRegistrationError("INVALID_JOB_RESULT") from None
        if len(json.dumps(parsed)) > MAX_OBSERVATION_BYTES:
            raise AgentRegistrationError("INVALID_JOB_RESULT")
        return {"code": code, "observation": parsed, "observed_at": _epoch(now)}
    if observation is not None:
        raise AgentRegistrationError("INVALID_JOB_RESULT")
    return {"code": code, "observed_at": _epoch(now)}


def same_report(stored: dict | None, new: dict) -> bool:
    """A repeated report (lost acknowledgement) is accepted when only the clock differs."""
    keys = ("code", "observation")
    return stored is not None and all(stored.get(k) == new.get(k) for k in keys)
