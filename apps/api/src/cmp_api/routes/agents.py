"""Operator-issued enrollment and authenticated outbound Agent polling."""

import hmac
from datetime import timedelta
from typing import Annotated, Literal

from cmp_contracts.chart import KubernetesName
from cmp_contracts.network_preflight import NetworkPreflightJob, ProbeObservation
from cmp_contracts.onprem_rollout import ProductionRolloutJob, RolloutReplication
from cmp_contracts.provisioning import Text
from cmp_providers.ecr_pull import EcrPullError, EcrTokenMinter, boto3_minter
from cmp_providers.env_file import EnvFileError, EnvFileStore
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from cmp_api.agent_registry import (
    AgentJob,
    AgentRegistrationError,
    QueuedWork,
    RegisteredAgent,
    authenticate_agent,
    create_enrollment,
    poll_job,
    redeem_enrollment,
    report_job,
    revoke_agent,
)
from cmp_api.config import settings
from cmp_api.db import SessionLocal
from cmp_api.env_store import configured_env_file_store
from cmp_api.onprem_rollout import (
    ArtifactStore,
    LocalArtifactStore,
    RolloutError,
    job_artifact,
    job_env_secrets,
    job_image_pull,
)

router = APIRouter(tags=["on-premises agents"])


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class EnrollmentRequest(Request):
    owner_id: Text
    agent_id: KubernetesName
    cluster_uid: Text | None = None  # omitted: bound to the UID reported at registration
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
    # NETWORK_PREFLIGHT only (MNP-01 N3): what the probe saw; codes/IPs/counters, no secrets.
    observation: ProbeObservation | None = None
    # PRODUCTION_ROLLOUT only (ticket 27): the DB replication-capable check after READY.
    replication: RolloutReplication | None = None


class ClaimedJob(Request):
    job_id: str
    payload: QueuedWork | ProductionRolloutJob | NetworkPreflightJob
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


class AgentAdminStatus(AgentStatus):
    last_seen_at: int | None
    revoked: bool


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
            result={"code": report.code}
            | (
                {"observation": report.observation.model_dump(mode="json")}
                if report.observation is not None
                else {}
            )
            | (
                {"replication": report.replication.model_dump(mode="json")}
                if report.replication is not None
                else {}
            ),
        )
    except AgentRegistrationError as exc:
        code = {"JOB_NOT_FOUND": 404, "AGENT_UNAUTHORIZED": 403}.get(str(exc), 409)
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


def get_env_store() -> EnvFileStore:
    try:
        return configured_env_file_store()
    except EnvFileError as exc:
        raise HTTPException(status_code=503, detail=exc.code) from None


class EnvSecret(Request):
    name: KubernetesName
    data: dict[str, str]


class EnvSecretsResponse(Request):
    secrets: list[EnvSecret]


@router.get(
    "/agents/{agent_id}/jobs/{job_id}/env-secrets",
    response_model=EnvSecretsResponse,
    responses={
        404: {"description": "`ENV_SECRETS_NOT_FOUND`: not this Agent's RUNNING EnvPlan job"},
        409: {"description": "Approval or `.env` check failed (code only, e.g. ENV_MISSING)"},
    },
)
def job_env_secrets_route(
    agent_id: str,
    job_id: str,
    session: SessionDep,
    credential: CredentialDep,
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    env_store: Annotated[EnvFileStore, Depends(get_env_store)],
):
    """ENV-04: the `<app>-env`/`<db>-conn` Secret data rendered from the approved EnvPlan for
    the caller's own RUNNING rollout job. Values only in this response (never in the job body,
    results, events or logs); the Agent applies them inside the user's cluster."""
    try:
        agent = authenticate_agent(session, agent_id=agent_id, credential=credential)
    except AgentRegistrationError:
        raise HTTPException(status_code=403, detail="Agent unauthorized") from None
    job = session.get(AgentJob, job_id)
    if job is None or job.agent_id != agent.id or job.owner_id != agent.owner_id:
        raise HTTPException(status_code=404, detail="ENV_SECRETS_NOT_FOUND")
    try:
        secrets = job_env_secrets(session, job, store, env_store)
    except RolloutError as exc:
        status = 404 if exc.code == "ENV_SECRETS_NOT_FOUND" else 409
        raise HTTPException(status_code=status, detail=exc.code) from None
    body = EnvSecretsResponse(secrets=[EnvSecret(name=s.name, data=s.reveal()) for s in secrets])
    return Response(
        content=body.model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def get_ecr_minter() -> EcrTokenMinter:
    """Ticket 29 (I2): the platform principal mints; the Agent never holds AWS credentials."""
    return boto3_minter(settings.connection_platform_aws_profile)


class ImagePullCredential(Request):
    registry: str
    username: str
    password: str
    expires_at: int


@router.get(
    "/agents/{agent_id}/jobs/{job_id}/image-pull",
    response_model=ImagePullCredential,
    responses={
        404: {"description": "`IMAGE_PULL_NOT_FOUND`: not this Agent's current ECR rollout job"},
        409: {"description": "Approved image outside the platform registry (code only)"},
        503: {"description": "Registry not configured or token unavailable (code only)"},
    },
)
def job_image_pull_route(
    agent_id: str,
    job_id: str,
    session: SessionDep,
    credential: CredentialDep,
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    minter: Annotated[EcrTokenMinter, Depends(get_ecr_minter)],
):
    """Ticket 29: a short-lived ECR pull token for the caller's own rollout job (RUNNING, or
    SUCCEEDED and still the latest approved Revision so the Agent can refresh the pull Secret).
    The token is only in this response (never in the job body, results, events or logs)."""
    try:
        agent = authenticate_agent(session, agent_id=agent_id, credential=credential)
    except AgentRegistrationError:
        raise HTTPException(status_code=403, detail="Agent unauthorized") from None
    job = session.get(AgentJob, job_id)
    if job is None or job.agent_id != agent.id or job.owner_id != agent.owner_id:
        raise HTTPException(status_code=404, detail="IMAGE_PULL_NOT_FOUND")
    try:
        registry = job_image_pull(session, job, store)
        pull = minter.mint(registry)
    except RolloutError as exc:
        status = 404 if exc.code == "IMAGE_PULL_NOT_FOUND" else 409
        if exc.code == "PLATFORM_REGISTRY_INVALID":
            status = 503
        raise HTTPException(status_code=status, detail=exc.code) from None
    except EcrPullError as exc:
        raise HTTPException(status_code=503, detail=exc.code) from None
    return Response(
        content=ImagePullCredential(**pull.response()).model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _admin_status(agent: RegisteredAgent) -> dict:
    return {
        "agent_id": agent.id,
        "owner_id": agent.owner_id,
        "cluster_uid": agent.cluster_uid,
        "last_seen_at": agent.last_seen_at,
        "revoked": agent.revoked_at is not None,
    }


@router.get(
    "/agent-admin/agents/{agent_id}",
    response_model=AgentAdminStatus,
    dependencies=[Depends(require_admin)],
)
def operator_agent_status(agent_id: str, session: SessionDep):
    """Operator view: last authenticated contact (epoch seconds) and revocation (ticket 15)."""
    agent = session.get(RegisteredAgent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="AGENT_NOT_FOUND")
    return _admin_status(agent)


@router.post(
    "/agent-admin/agents/{agent_id}/revoke",
    response_model=AgentAdminStatus,
    dependencies=[Depends(require_admin)],
)
def operator_revoke_agent(agent_id: str, session: SessionDep):
    try:
        revoke_agent(session, agent_id=agent_id)
    except AgentRegistrationError:
        raise HTTPException(status_code=404, detail="AGENT_NOT_FOUND") from None
    return _admin_status(session.get(RegisteredAgent, agent_id))
