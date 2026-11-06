"""Operator control of Agent jobs: cancel, expiry and environment teardown (ticket 52, D3–D10).

- Expiry/cancel only ever close work that has not started (`QUEUED`). A `RUNNING` job is marked
  `cancel_requested_at`: its downloads stop, a reclaim closes it as `UNKNOWN`, but a Helm step
  already in progress finishes and its report is recorded.
- A teardown job is derived from an earlier rollout job of the same Agent, so it can only name
  the Namespace/Releases that job deployed. Data (PVC, Namespace) is removed only on request.
Codes only; no Secret names beyond those already in the rollout job, no values.
"""

from datetime import UTC, datetime

from cmp_contracts.onprem_rollout import ROLLOUT_MODE, ProductionRolloutJob
from cmp_contracts.onprem_teardown import TEARDOWN_MODE, TeardownJob, teardown_job_id
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cmp_api.agent_registry import AgentJob, RegisteredAgent

ACTIVE = {"QUEUED", "RUNNING"}
FINISHED = {"SUCCEEDED", "FAILED", "UNKNOWN", "EXPIRED", "CANCELLED"}
TEARDOWN_SECONDS = 3600  # D2: an operator request the Agent must start within an hour


class AgentJobError(ValueError):
    """Code-only error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _epoch(now: datetime | None) -> int:
    return int((now or datetime.now(UTC)).timestamp())


def is_teardown(payload) -> bool:
    return isinstance(payload, dict) and payload.get("mode") == TEARDOWN_MODE


def _mode(job: AgentJob) -> str | None:
    return job.payload.get("mode") if isinstance(job.payload, dict) else None


def expire_if_queued(session: Session, job_id: str, code: str) -> bool:
    """D3: close a job nobody has started yet. Conditional, so it never races a claim."""
    result = session.execute(
        update(AgentJob)
        .where(AgentJob.id == job_id, AgentJob.status == "QUEUED")
        .values(status="EXPIRED", result={"code": code})
    )
    session.commit()
    return result.rowcount == 1


def cancel_job(session: Session, job_id: str, *, now: datetime | None = None) -> AgentJob:
    """D5: operator cancel of a rollout or teardown job."""
    job = session.get(AgentJob, job_id)
    if job is None:
        raise AgentJobError("JOB_NOT_FOUND")
    if _mode(job) not in {ROLLOUT_MODE, TEARDOWN_MODE}:
        raise AgentJobError("CANCEL_NOT_SUPPORTED")
    if job.status == "QUEUED":
        cancelled = session.execute(
            update(AgentJob)
            .where(AgentJob.id == job_id, AgentJob.status == "QUEUED")
            .values(status="CANCELLED", result={"code": "JOB_CANCELLED"})
        )
        session.commit()
        if cancelled.rowcount == 1:
            session.refresh(job)
            return job
        session.refresh(job)  # claimed meanwhile: fall through to the RUNNING case
    if job.status == "RUNNING":
        if job.cancel_requested_at is None:
            job.cancel_requested_at = _epoch(now)
            session.commit()
        return job
    raise AgentJobError("JOB_ALREADY_FINISHED")


def running_block(job: AgentJob) -> str | None:
    """D5: downloads for a RUNNING job stop once a cancel was requested."""
    return "JOB_CANCELLED" if job.cancel_requested_at is not None else None


def _namespace(job: AgentJob) -> str | None:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return (
        payload.get("namespace") if payload.get("mode") in {ROLLOUT_MODE, TEARDOWN_MODE} else None
    )


def active_in_namespace(session: Session, agent_id: str, namespace: str, mode: str) -> bool:
    """D10: is a job of `mode` for this Agent's Namespace queued or running?"""
    jobs = session.scalars(
        select(AgentJob).where(AgentJob.agent_id == agent_id, AgentJob.status.in_(ACTIVE))
    )
    return any(_mode(job) == mode and _namespace(job) == namespace for job in jobs)


def create_teardown(
    session: Session, source_job_id: str, *, delete_data: bool, now: datetime | None = None
) -> AgentJob:
    """D7: queue (or reuse) the teardown of what `source_job_id` deployed."""
    source = session.get(AgentJob, source_job_id)
    try:
        rollout = ProductionRolloutJob.model_validate(source.payload) if source else None
    except ValidationError:
        rollout = None
    if rollout is None:
        raise AgentJobError("TEARDOWN_SOURCE_INVALID")
    agent = session.get(RegisteredAgent, source.agent_id)
    if agent is None or agent.revoked_at is not None:
        raise AgentJobError("AGENT_REVOKED")
    if active_in_namespace(session, agent.id, rollout.namespace, ROLLOUT_MODE):
        raise AgentJobError("ROLLOUT_IN_PROGRESS")
    job_id = teardown_job_id(source.id, delete_data)
    payload = TeardownJob(
        mode=TEARDOWN_MODE,
        job_id=job_id,
        source_job_id=source.id,
        environment_id=rollout.environment_id,
        agent_id=agent.id,
        cluster_uid=agent.cluster_uid,
        namespace=rollout.namespace,
        releases=[rollout.application.release, rollout.database.release],
        database_release=rollout.database.release,
        database_secret=rollout.database.password_secret.name,
        delete_data=delete_data,
    ).model_dump(mode="json")
    deadline = _epoch(now) + TEARDOWN_SECONDS
    job = session.get(AgentJob, job_id)
    if job is None:
        session.add(
            AgentJob(
                id=job_id,
                owner_id=source.owner_id,
                agent_id=agent.id,
                payload=payload,
                status="QUEUED",
                deadline_at=deadline,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        job = session.get(AgentJob, job_id)
    elif job.status in FINISHED - {"SUCCEEDED"}:
        # A teardown is idempotent: an operator may retry one that did not finish.
        job.payload, job.status, job.result = payload, "QUEUED", None
        job.deadline_at, job.cancel_requested_at = deadline, None
        session.commit()
    return job


def teardown_block(session: Session, job: AgentJob) -> str | None:
    """Claim gate: still this registered Agent/cluster, and no rollout in that Namespace."""
    try:
        teardown = TeardownJob.model_validate(job.payload)
    except ValidationError:
        return "INVALID_TEARDOWN_JOB"
    agent = session.get(RegisteredAgent, job.agent_id)
    if (
        agent is None
        or agent.revoked_at is not None
        or teardown.agent_id != agent.id
        or teardown.cluster_uid != agent.cluster_uid
    ):
        return "AGENT_TARGET_MISMATCH"
    if active_in_namespace(session, agent.id, teardown.namespace, ROLLOUT_MODE):
        return "ROLLOUT_IN_PROGRESS"
    return None


def job_view(job: AgentJob) -> dict:
    return {
        "job_id": job.id,
        "mode": _mode(job),
        "agent_id": job.agent_id,
        "status": job.status,
        "code": (job.result or {}).get("code"),
        "cancel_requested": job.cancel_requested_at is not None,
    }
