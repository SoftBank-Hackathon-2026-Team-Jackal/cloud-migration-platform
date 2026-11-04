"""Approved ON_PREM rollout dispatch through the owner-scoped Agent transport (ticket 12).

The provider execution step receives 08's claim result, re-reads the business DB Plan Revision,
verifies the exact stored artifact bytes it derives the job from, and queues at most one Agent
job per Plan Revision. The Agent claim/download path re-checks the approval again (E5).
Only codes and references leave this module; no Values bodies or Secret values.
"""

import re
from pathlib import Path
from typing import Protocol

from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.chart import PostgresChartValues
from cmp_contracts.onprem_rollout import (
    ROLLOUT_MODE,
    ProductionRolloutJob,
    RolloutApplication,
    RolloutConnectionUrl,
    RolloutDatabase,
    rollout_job_id,
)
from cmp_contracts.plan import PlanManifest
from cmp_contracts.shared_deployment_v2 import ApplicationValuesV2, SecretRefV2
from cmp_providers.artifacts import sha256_bytes, verify_artifact_bytes
from cmp_providers.database_url import URL_SECRET_KEY, connection_url_secret_name
from cmp_providers.plans import manifest_digest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cmp_api.agent_registry import AgentJob, RegisteredAgent
from cmp_api.approvals import PlanApproval, PlanRevision, latest_revision

TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN"}
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class RolloutError(ValueError):
    """Code-only error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ArtifactStore(Protocol):
    def read(self, ref: ArtifactRef) -> bytes: ...

    def read_raw(self, sha256: str) -> bytes: ...


class LocalArtifactStore:
    """P0 platform artifact store: exact bytes under their SHA-256 filename (S3 later).

    IDs are metadata only; the validated digest is the sole path component.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, sha256: str) -> Path:
        if not _SHA256.fullmatch(sha256):
            raise RolloutError("ARTIFACT_UNAVAILABLE")
        return self.root / sha256

    def write_raw(self, sha256: str, content: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(sha256).write_bytes(content)

    def put(self, content: bytes, *, ref_id: str, kind: str, version: str = "1") -> dict:
        digest = sha256_bytes(content)
        self.write_raw(digest, content)
        return {
            "id": ref_id,
            "version": version,
            "kind": kind,
            "sha256": digest,
            "size_bytes": len(content),
        }

    def read_raw(self, sha256: str) -> bytes:
        try:
            return self._path(sha256).read_bytes()
        except OSError:
            raise RolloutError("ARTIFACT_UNAVAILABLE") from None

    def read(self, ref: ArtifactRef) -> bytes:
        content = self.read_raw(ref.sha256)
        try:
            verify_artifact_bytes(ref, content)
        except ValueError:
            raise RolloutError("ARTIFACT_DIGEST_MISMATCH") from None
        return content


def _approved_revision(
    session: Session, *, approval_id: str, plan_id: str, revision: int, digest: str
) -> PlanManifest:
    """Business-DB approval gate shared by dispatch and the Agent claim/download path."""
    record = session.get(PlanApproval, approval_id)
    if (
        record is None
        or record.decision != "APPROVE"
        or record.plan_id != plan_id
        or record.revision != revision
    ):
        raise RolloutError("PLAN_NOT_APPROVED")
    if revision != latest_revision(session, plan_id):
        raise RolloutError("STALE_REVISION")
    row = session.get(PlanRevision, (plan_id, revision))
    manifest = PlanManifest.model_validate(row.manifest)
    current = manifest_digest(manifest)
    if current != record.manifest_digest or current != digest:
        raise RolloutError("APPROVAL_TARGET_MISMATCH")
    return manifest


def _values(store: ArtifactStore, manifest: PlanManifest):
    try:
        database = PostgresChartValues.model_validate_json(
            store.read(manifest.artifacts["database_values"])
        )
        application = ApplicationValuesV2.model_validate_json(
            store.read(manifest.artifacts["application_values"])
        )
        for role in ("database_chart", "application_chart"):
            store.read(manifest.artifacts[role])
    except KeyError:
        raise RolloutError("ARTIFACT_MISSING") from None
    except ValidationError:
        raise RolloutError("INVALID_VALUES_ARTIFACT") from None
    target = manifest.target
    if (
        database.environment != "PRODUCTION"
        or application.environment != "PRODUCTION"
        or database.provider != "ON_PREM"
        or application.provider != "ON_PREM"
        or database.namespace != target.namespace
        or application.namespace != target.namespace
    ):
        raise RolloutError("VALUES_TARGET_MISMATCH")
    return database, application


def build_rollout_job(
    session: Session, request: dict, store: ArtifactStore
) -> tuple[ProductionRolloutJob, str]:
    """Returns (job, owner_id). Raises RolloutError before anything is queued."""
    if request.get("provider") != "ON_PREM":
        raise RolloutError("PROVIDER_MISMATCH")
    manifest = _approved_revision(
        session,
        approval_id=request["approval_id"],
        plan_id=request["plan_id"],
        revision=request["revision"],
        digest=request["manifest_digest"],
    )
    target = manifest.target
    if target.provider != "ON_PREM":
        raise RolloutError("PROVIDER_MISMATCH")
    if manifest.scope != request.get("scope", manifest.scope):
        raise RolloutError("APPROVAL_TARGET_MISMATCH")
    database, application = _values(store, manifest)
    agent = session.get(RegisteredAgent, target.agent_id)
    if agent is None or agent.cluster_uid != target.cluster_ref.id:
        raise RolloutError("AGENT_TARGET_MISMATCH")
    url_secret = (connection_url_secret_name(database.release_name), URL_SECRET_KEY)
    secret_sources = {
        (env.source.name, env.source.key) for env in application.env if env.source.kind == "SECRET"
    }
    secret_refs = {
        ref
        for ref in secret_sources
        # DB password and URL Secrets are prepared by the Agent; the rest must already exist.
        if ref[0] != database.database.secret_name and ref != url_secret
    }
    connection_url = (
        RolloutConnectionUrl(
            secret=SecretRefV2(name=url_secret[0], key=url_secret[1]),
            database=database.database.name,
            username=database.database.username,
        )
        if url_secret in secret_sources
        else None
    )
    job = ProductionRolloutJob(
        mode=ROLLOUT_MODE,
        job_id=rollout_job_id(manifest.plan.plan_id, manifest.plan.revision),
        approval_id=request["approval_id"],
        plan_id=manifest.plan.plan_id,
        revision=manifest.plan.revision,
        manifest_digest=request["manifest_digest"],
        environment_id=target.environment_id,
        agent_id=agent.id,
        cluster_uid=agent.cluster_uid,
        namespace=target.namespace,
        storage_class=database.storage.class_name,
        database=RolloutDatabase(
            release=database.release_name,
            chart=manifest.artifacts["database_chart"],
            values=manifest.artifacts["database_values"],
            password_secret=SecretRefV2(
                name=database.database.secret_name, key=database.database.secret_key
            ),
            connection_url=connection_url,
        ),
        application=RolloutApplication(
            release=application.release_name,
            chart=manifest.artifacts["application_chart"],
            values=manifest.artifacts["application_values"],
            port=application.application.port,
            health_path=application.application.health_path,
            secret_refs=[SecretRefV2(name=n, key=k) for n, k in sorted(secret_refs)],
        ),
    )
    return job, agent.owner_id


def dispatch(session: Session, job: ProductionRolloutJob, owner_id: str) -> AgentJob:
    """Queue the Revision's job once; an existing job is reused only if it is identical."""
    payload = job.model_dump(mode="json")
    existing = session.get(AgentJob, job.job_id)
    if existing is None:
        session.add(
            AgentJob(
                id=job.job_id,
                owner_id=owner_id,
                agent_id=job.agent_id,
                payload=payload,
                status="QUEUED",
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        existing = session.get(AgentJob, job.job_id)
    if existing.payload != payload or existing.agent_id != job.agent_id:
        raise RolloutError("AGENT_JOB_CONFLICT")
    return existing


def check_dispatchable(session: Session, payload: dict) -> str | None:
    """Agent claim/download gate. Returns a blocking code or None."""
    try:
        job = ProductionRolloutJob.model_validate(payload)
        _approved_revision(
            session,
            approval_id=job.approval_id,
            plan_id=job.plan_id,
            revision=job.revision,
            digest=job.manifest_digest,
        )
    except ValidationError:
        return "INVALID_ROLLOUT_JOB"
    except RolloutError as exc:
        return exc.code
    return None


def is_rollout(payload: dict) -> bool:
    return isinstance(payload, dict) and payload.get("mode") == ROLLOUT_MODE


def job_artifact(session: Session, job: AgentJob, sha256: str, store: ArtifactStore) -> bytes:
    """Bytes for one artifact referenced by this RUNNING rollout job, verified by digest."""
    if job.status != "RUNNING" or not is_rollout(job.payload):
        raise RolloutError("ARTIFACT_NOT_FOUND")
    rollout = ProductionRolloutJob.model_validate(job.payload)
    refs = [ref for ref in rollout.artifact_refs() if ref.sha256 == sha256]
    if not refs:
        raise RolloutError("ARTIFACT_NOT_FOUND")
    if check_dispatchable(session, job.payload) is not None:
        raise RolloutError("ARTIFACT_NOT_FOUND")
    return store.read(refs[0])


_STATE = {
    "QUEUED": "WAITING_AGENT",
    "RUNNING": "RUNNING",
    "FAILED": "FAILED",
    "UNKNOWN": "UNKNOWN",
}


def rollout_status(session: Session, plan_id: str, revision: int) -> dict:
    job = session.get(AgentJob, rollout_job_id(plan_id, revision))
    if job is None:
        return {"state": "NOT_DISPATCHED", "code": None, "job_id": None, "agent_id": None}
    code = (job.result or {}).get("code")
    state = _STATE.get(job.status, "UNKNOWN")
    if job.status == "SUCCEEDED":
        state = "READY" if code == "READY" else "UNKNOWN"
    return {"state": state, "code": code, "job_id": job.id, "agent_id": job.agent_id}


def resource_refs(job: ProductionRolloutJob, *, ready: bool) -> dict[str, str]:
    refs = {"job_id": job.job_id}
    if ready:
        ns = job.namespace
        refs |= {
            "namespace": ns,
            "database_release": job.database.release,
            "application_release": job.application.release,
            "database_endpoint": f"{job.database.release}.{ns}.svc:5432",
            "application_endpoint": f"{job.application.release}.{ns}.svc:{job.application.port}",
            "database_secret": f"{job.database.password_secret.name}/"
            f"{job.database.password_secret.key}",
        }
        # Ticket 13 R4: standard handoff keys (legacy keys above stay for ticket 12 readers).
        refs |= {
            "agent_id": job.agent_id,
            "cluster_uid": job.cluster_uid,
            "storage_class": job.storage_class,
            "db_release": job.database.release,
            "db_host": f"{job.database.release}.{ns}.svc",
            "db_port": "5432",
            "db_secret_ref": refs["database_secret"],
            "app_release": job.application.release,
            "app_port": str(job.application.port),
            "app_health_path": job.application.health_path,
            "app_internal_endpoint": f"http://{refs['application_endpoint']}",
        }
    return refs


def values_refs(session: Session, store: "ArtifactStore", plan_id: str, revision: int) -> dict:
    """Ticket 13 R4: non-secret DB identity and replica count from the approved Values bytes."""
    row = session.get(PlanRevision, (plan_id, revision))
    if row is None:
        return {}
    try:
        database, application = _values(store, PlanManifest.model_validate(row.manifest))
    except RolloutError:
        return {}
    return {
        "db_name": database.database.name,
        "db_user": database.database.username,
        "db_version": database.database.version,
        "db_storage_size_gib": str(database.storage.size_gib),
        "app_replicas": str(application.application.replicas),
    }


def outcome(job: AgentJob) -> dict | None:
    """Activity result once the Agent job is terminal; None while QUEUED/RUNNING."""
    if job.status not in TERMINAL:
        return None
    rollout = ProductionRolloutJob.model_validate(job.payload)
    code = (job.result or {}).get("code", "AGENT_RESULT_MISSING")
    ready = job.status == "SUCCEEDED" and code == "READY"
    status = job.status if job.status != "SUCCEEDED" or ready else "UNKNOWN"
    return {"status": status, "code": code, "resource_refs": resource_refs(rollout, ready=ready)}
