"""Persistent owner-scoped Agent enrollment and reference-only work transport."""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from cmp_contracts.provisioning import Contract, VersionRef
from pydantic import ValidationError
from sqlalchemy import JSON, ForeignKey, Index, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from cmp_api.db import Base


class AgentRegistrationError(ValueError):
    """Code-only error; never contains tokens or credentials."""


class AgentOwner(Base):
    __tablename__ = "agent_owners"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)


class Enrollment(Base):
    __tablename__ = "agent_enrollments"

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("agent_owners.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    cluster_uid: Mapped[str] = mapped_column(String(120), nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    used_at: Mapped[int | None] = mapped_column(Integer)


class RegisteredAgent(Base):
    __tablename__ = "registered_agents"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("agent_owners.id"), nullable=False)
    cluster_uid: Mapped[str] = mapped_column(String(120), nullable=False)
    credential_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class AgentJob(Base):
    __tablename__ = "agent_jobs"
    __table_args__ = (Index("ix_agent_jobs_agent_status", "agent_id", "status"),)

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("agent_owners.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(ForeignKey("registered_agents.id"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED")
    result: Mapped[dict | None] = mapped_column(JSON)


class QueuedWork(Contract):
    kind: Literal["SANDBOX_TEST"]
    work_ref: VersionRef


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _epoch(now: datetime | None) -> int:
    return int((now or datetime.now(UTC)).timestamp())


def create_enrollment(
    session: Session,
    *,
    owner_id: str,
    agent_id: str,
    cluster_uid: str,
    ttl: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
) -> str:
    if not owner_id or not agent_id or not cluster_uid or not 0 < ttl.total_seconds() <= 3600:
        raise AgentRegistrationError("INVALID_ENROLLMENT")
    if session.get(RegisteredAgent, agent_id) or session.scalar(
        select(Enrollment).where(Enrollment.agent_id == agent_id)
    ):
        raise AgentRegistrationError("AGENT_ID_RESERVED")
    token = "cmp_enroll_" + secrets.token_urlsafe(32)
    if session.get(AgentOwner, owner_id) is None:
        session.add(AgentOwner(id=owner_id))
    session.add(
        Enrollment(
            token_digest=_digest(token),
            owner_id=owner_id,
            agent_id=agent_id,
            cluster_uid=cluster_uid,
            expires_at=_epoch(now) + int(ttl.total_seconds()),
        )
    )
    session.commit()
    return token


def redeem_enrollment(
    session: Session,
    *,
    token: str,
    agent_id: str,
    cluster_uid: str,
    now: datetime | None = None,
) -> str:
    enrollment = session.scalar(
        select(Enrollment).where(Enrollment.token_digest == _digest(token)).with_for_update()
    )
    if (
        enrollment is None
        or enrollment.used_at is not None
        or enrollment.expires_at <= _epoch(now)
        or enrollment.agent_id != agent_id
        or enrollment.cluster_uid != cluster_uid
        or session.get(RegisteredAgent, agent_id) is not None
    ):
        raise AgentRegistrationError("INVALID_ENROLLMENT")
    credential = "cmp_agent_" + secrets.token_urlsafe(32)
    enrollment.used_at = _epoch(now)
    session.add(
        RegisteredAgent(
            id=agent_id,
            owner_id=enrollment.owner_id,
            cluster_uid=cluster_uid,
            credential_digest=_digest(credential),
        )
    )
    session.commit()
    return credential


def authenticate_agent(session: Session, *, agent_id: str, credential: str) -> RegisteredAgent:
    agent = session.get(RegisteredAgent, agent_id)
    if agent is None or not hmac.compare_digest(agent.credential_digest, _digest(credential)):
        raise AgentRegistrationError("AGENT_UNAUTHORIZED")
    return agent


def enqueue_job(
    session: Session, *, job_id: str, owner_id: str, agent_id: str, payload: dict
) -> None:
    agent = session.get(RegisteredAgent, agent_id)
    try:
        work = QueuedWork.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        raise AgentRegistrationError("INVALID_WORK_REFERENCE") from None
    if agent is None or agent.owner_id != owner_id:
        raise AgentRegistrationError("JOB_TARGET_MISMATCH")
    if session.get(AgentJob, job_id) is not None:
        raise AgentRegistrationError("JOB_ID_RESERVED")
    session.add(
        AgentJob(
            id=job_id,
            owner_id=owner_id,
            agent_id=agent_id,
            payload=work.model_dump(mode="json"),
            status="QUEUED",
        )
    )
    session.commit()


def _dispatch_block(session: Session, job: AgentJob) -> str | None:
    """Approved ON_PREM rollout jobs are re-checked against the business DB on every claim."""
    from cmp_api.onprem_rollout import check_dispatchable, is_rollout  # avoid import cycle

    return check_dispatchable(session, job.payload) if is_rollout(job.payload) else None


def poll_job(session: Session, *, agent_id: str, credential: str) -> dict | None:
    agent = authenticate_agent(session, agent_id=agent_id, credential=credential)
    while True:
        job = session.scalar(
            select(AgentJob)
            .where(AgentJob.agent_id == agent.id, AgentJob.status == "QUEUED")
            .order_by(AgentJob.id)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            break
        blocked = _dispatch_block(session, job)
        if blocked:
            # Never hand an unapproved/superseded Plan to the Agent; nothing has run yet.
            job.status, job.result = "FAILED", {"code": blocked}
            session.commit()
            continue
        job.status = "RUNNING"
        session.commit()
        return {"job_id": job.id, "payload": job.payload, "status": job.status}
    for job in session.scalars(
        select(AgentJob)
        .where(AgentJob.agent_id == agent.id, AgentJob.status == "RUNNING")
        .order_by(AgentJob.id)
    ):
        blocked = _dispatch_block(session, job)
        if blocked:
            # It may have partially applied; require inspection instead of a silent re-run.
            job.status, job.result = "UNKNOWN", {"code": blocked}
            session.commit()
            continue
        return {"job_id": job.id, "payload": job.payload, "status": job.status}
    return None


def report_job(
    session: Session,
    *,
    agent_id: str,
    credential: str,
    job_id: str,
    status: str,
    result: dict,
) -> None:
    agent = authenticate_agent(session, agent_id=agent_id, credential=credential)
    job = session.get(AgentJob, job_id)
    if job is None or job.agent_id != agent.id or job.owner_id != agent.owner_id:
        raise AgentRegistrationError("JOB_NOT_FOUND")
    if status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
        raise AgentRegistrationError("INVALID_JOB_STATUS")
    if (
        set(result) != {"code"}
        or not isinstance(result["code"], str)
        or not re.fullmatch(r"[A-Z0-9_]{1,64}", result["code"])
    ):
        raise AgentRegistrationError("INVALID_JOB_RESULT")
    if job.status == status and job.result == result:
        return
    if job.status != "RUNNING":
        raise AgentRegistrationError("INVALID_JOB_STATUS")
    job.status = status
    job.result = result
    session.commit()
