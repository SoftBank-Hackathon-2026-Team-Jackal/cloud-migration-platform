"""Operator-issued enrollment and authenticated outbound Agent polling."""

import hmac
from datetime import timedelta
from typing import Annotated, Literal

from cmp_contracts.chart import KubernetesName
from cmp_contracts.onprem_rollout import ProductionRolloutJob
from cmp_contracts.provisioning import Text
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from cmp_api.agent_registry import (
    AgentJob,
    AgentRegistrationError,
    QueuedWork,
    authenticate_agent,
    create_enrollment,
    poll_job,
    redeem_enrollment,
    report_job,
)
from cmp_api.config import settings
from cmp_api.db import SessionLocal
from cmp_api.onprem_rollout import ArtifactStore, LocalArtifactStore, RolloutError, job_artifact

router = APIRouter(tags=["on-premises agents"])


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class EnrollmentRequest(Request):
    owner_id: Text
    agent_id: KubernetesName
    cluster_uid: Text
    ttl_seconds: int = Field(default=600, ge=1, le=3600)


class EnrollmentResponse(Request):
    enrollment_token: str
    expires_in_seconds: int


class RegistrationRequest(Request):
    enrollment_token: str
    agent_id: KubernetesName
    cluster_uid: Text


class RegistrationResponse(Request):
    agent_id: str
    credential: str


class JobReport(Request):
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    code: str = Field(pattern=r"^[A-Z0-9_]{1,64}$")


class ClaimedJob(Request):
    job_id: str
    payload: QueuedWork | ProductionRolloutJob
    status: Literal["RUNNING"]


class ClaimResponse(Request):
    job: ClaimedJob | None


class ReportAck(Request):
    job_id: str
    accepted: Literal[True]


class AgentStatus(Request):
    agent_id: str
    owner_id: str
    cluster_uid: str


def get_session():
    with SessionLocal() as session:
        yield session


def require_admin(x_enrollment_admin_key: str | None = Header(default=None)) -> None:
    expected = settings.agent_enrollment_admin_key
    if (
        not expected
        or not x_enrollment_admin_key
        or not hmac.compare_digest(expected, x_enrollment_admin_key)
    ):
        raise HTTPException(status_code=403, detail="Enrollment administration unavailable")


def bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Agent authentication required")
    credential = authorization.removeprefix("Bearer ")
    if not credential:
        raise HTTPException(status_code=401, detail="Agent authentication required")
    return credential


def get_artifact_store() -> ArtifactStore:
    if not settings.artifact_store_dir:
        raise HTTPException(status_code=503, detail="Artifact store unavailable")
    return LocalArtifactStore(settings.artifact_store_dir)


SessionDep = Annotated[Session, Depends(get_session)]
CredentialDep = Annotated[str, Depends(bearer)]


@router.post(
    "/agent-enrollments", response_model=EnrollmentResponse, dependencies=[Depends(require_admin)]
)
def issue_enrollment(request: EnrollmentRequest, session: SessionDep):
    try:
        token = create_enrollment(
            session,
            owner_id=request.owner_id,
            agent_id=request.agent_id,
            cluster_uid=request.cluster_uid,
            ttl=timedelta(seconds=request.ttl_seconds),
        )
    except AgentRegistrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return EnrollmentResponse(enrollment_token=token, expires_in_seconds=request.ttl_seconds)


@router.post("/agents/register", response_model=RegistrationResponse)
def register_agent(request: RegistrationRequest, session: SessionDep):
    try:
        credential = redeem_enrollment(
            session,
            token=request.enrollment_token,
            agent_id=request.agent_id,
            cluster_uid=request.cluster_uid,
        )
    except AgentRegistrationError:
        raise HTTPException(status_code=403, detail="Invalid enrollment") from None
    return RegistrationResponse(agent_id=request.agent_id, credential=credential)


@router.post("/agents/{agent_id}/jobs/claim", response_model=ClaimResponse)
def next_job(
    agent_id: str,
    session: SessionDep,
    credential: CredentialDep,
):
    try:
        return {"job": poll_job(session, agent_id=agent_id, credential=credential)}
    except AgentRegistrationError:
        raise HTTPException(status_code=403, detail="Agent unauthorized") from None


@router.post("/agents/{agent_id}/jobs/{job_id}/result", response_model=ReportAck, status_code=202)
def submit_result(
    agent_id: str,
    job_id: str,
    report: JobReport,
    session: SessionDep,
    credential: CredentialDep,
):
    try:
        report_job(
            session,
            agent_id=agent_id,
            credential=credential,
            job_id=job_id,
            status=report.status,
            result={"code": report.code},
        )
    except AgentRegistrationError as exc:
        code = 404 if str(exc) == "JOB_NOT_FOUND" else 409
        raise HTTPException(status_code=code, detail=str(exc)) from None
    return {"job_id": job_id, "accepted": True}


@router.get("/agents/{agent_id}", response_model=AgentStatus)
def agent_status(
    agent_id: str,
    session: SessionDep,
    credential: CredentialDep,
):
    try:
        agent = authenticate_agent(session, agent_id=agent_id, credential=credential)
    except AgentRegistrationError:
        raise HTTPException(status_code=403, detail="Agent unauthorized") from None
    return {"agent_id": agent.id, "owner_id": agent.owner_id, "cluster_uid": agent.cluster_uid}


@router.get(
    "/agents/{agent_id}/jobs/{job_id}/artifacts/{sha256}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def job_artifact_bytes(
    agent_id: str,
    job_id: str,
    session: SessionDep,
    credential: CredentialDep,
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    sha256: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
):
    """Exact approved Chart/Values bytes for the caller's own RUNNING rollout job (ticket 12)."""
    try:
        agent = authenticate_agent(session, agent_id=agent_id, credential=credential)
    except AgentRegistrationError:
        raise HTTPException(status_code=403, detail="Agent unauthorized") from None
    job = session.get(AgentJob, job_id)
    if job is None or job.agent_id != agent.id or job.owner_id != agent.owner_id:
        raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
    try:
        content = job_artifact(session, job, sha256, store)
    except RolloutError as exc:
        status = 404 if exc.code == "ARTIFACT_NOT_FOUND" else 409
        raise HTTPException(status_code=status, detail=exc.code) from None
    return Response(content=content, media_type="application/octet-stream")
