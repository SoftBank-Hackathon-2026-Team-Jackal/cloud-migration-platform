"""Business-DB Plan Revision approval records and the guarded execution boundary (ticket 07).

The approval record identifies an exact Plan ID, Revision and Digest via the shared CP-01
contract (`approval_target`). The execution boundary re-reads the record and the latest stored
Revision immediately before calling an executor, so neither a Signal nor a client-supplied
target is trusted on its own. No Terraform, Helm or Agent execution is wired here.
"""

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from cmp_contracts.plan import ApprovalTarget, PlanManifest, Preview, Scope
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.artifacts import sha256_json
from cmp_providers.plans import (
    approval_issues,
    approval_target,
    manifest_digest,
    verify_approval_target,
)
from sqlalchemy import (
    JSON,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from cmp_api.db import Base

Decision = Literal["APPROVE", "REJECT"]
EXECUTION_STEP = "PROVISION"


class ApprovalError(ValueError):
    """Code-only error; never contains credentials, Plan contents or secrets."""

    def __init__(self, code: str, issues: list[str] | None = None):
        super().__init__(code)
        self.code = code
        self.issues = issues or []


class PlanRevision(Base):
    __tablename__ = "provisioning_plan_revisions"

    plan_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    migration_id: Mapped[str] = mapped_column(String(120), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    preview: Mapped[dict] = mapped_column(JSON, nullable=False)
    published_at: Mapped[int] = mapped_column(Integer, nullable=False)


class Approver(Base):
    __tablename__ = "provisioning_approvers"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    credential_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    migration_ids: Mapped[list] = mapped_column(JSON, nullable=False)


class PlanApproval(Base):
    __tablename__ = "provisioning_approvals"
    __table_args__ = (
        UniqueConstraint("plan_id", "revision", name="uq_provisioning_approvals_revision"),
        ForeignKeyConstraint(
            ["plan_id", "revision"],
            ["provisioning_plan_revisions.plan_id", "provisioning_plan_revisions.revision"],
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(120), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    approver_id: Mapped[str] = mapped_column(
        ForeignKey("provisioning_approvers.id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(12), nullable=False)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_error: Mapped[str | None] = mapped_column(String(64))


class PlanExecution(Base):
    """Unique execution marker; its existence means the executor may already have run."""

    __tablename__ = "provisioning_executions"

    plan_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    step: Mapped[str] = mapped_column(String(40), primary_key=True)
    approval_id: Mapped[str] = mapped_column(
        ForeignKey("provisioning_approvals.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_at: Mapped[int | None] = mapped_column(Integer)


def _now(now: datetime | None = None) -> int:
    return int((now or datetime.now(UTC)).timestamp())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_revision(session: Session, plan_id: str) -> int | None:
    return session.scalar(
        select(func.max(PlanRevision.revision)).where(PlanRevision.plan_id == plan_id)
    )


def publish_plan_revision(session: Session, manifest: PlanManifest, preview: Preview) -> None:
    """Store an immutable Plan Revision. Planning integration calls this later; no HTTP route."""
    manifest = PlanManifest.model_validate(manifest.model_dump(mode="json"))
    preview = Preview.model_validate(preview.model_dump(mode="json"))
    current = latest_revision(session, manifest.plan.plan_id)
    if current is not None and manifest.plan.revision <= current:
        raise ApprovalError("REVISION_NOT_INCREASING")
    session.add(
        PlanRevision(
            plan_id=manifest.plan.plan_id,
            revision=manifest.plan.revision,
            migration_id=manifest.migration_id,
            manifest=manifest.model_dump(mode="json"),
            preview=preview.model_dump(mode="json"),
            published_at=_now(),
        )
    )
    session.commit()


def register_approver(session: Session, *, approver_id: str, migration_ids: list[str]) -> str:
    """Operator-only registration. Returns the credential once; only its digest is stored."""
    if session.get(Approver, approver_id) is not None:
        raise ApprovalError("APPROVER_EXISTS")
    credential = "cmp_approver_" + secrets.token_urlsafe(32)
    session.add(
        Approver(
            id=approver_id,
            credential_digest=_digest(credential),
            migration_ids=sorted(set(migration_ids)),
        )
    )
    session.commit()
    return credential


def authenticate_approver(session: Session, credential: str) -> Approver:
    approver = session.scalar(
        select(Approver).where(Approver.credential_digest == _digest(credential))
    )
    if approver is None or not hmac.compare_digest(approver.credential_digest, _digest(credential)):
        raise ApprovalError("APPROVER_AUTH_REQUIRED")
    return approver


def _models(row: PlanRevision) -> tuple[PlanManifest, Preview]:
    return PlanManifest.model_validate(row.manifest), Preview.model_validate(row.preview)


def _stored_identity(manifest: PlanManifest, preview: Preview) -> ApprovalTarget:
    return ApprovalTarget(
        plan_id=manifest.plan.plan_id,
        revision=manifest.plan.revision,
        scope=manifest.scope,
        manifest_digest=manifest_digest(manifest),
        preview_digest=sha256_json(preview.model_dump(mode="json")),
    )


def _existing_decision(
    session: Session, plan_id: str, revision: int, decision: Decision
) -> PlanApproval | None:
    existing = session.scalar(
        select(PlanApproval).where(
            PlanApproval.plan_id == plan_id, PlanApproval.revision == revision
        )
    )
    if existing is not None and existing.decision != decision:
        raise ApprovalError("ALREADY_DECIDED")
    return existing


def record_decision(
    session: Session,
    *,
    approver: Approver,
    plan_id: str,
    revision: int,
    decision: Decision,
    scope: Scope,
    manifest_digest: str,
    preview_digest: str,
) -> tuple[PlanApproval, bool]:
    """Validate and durably record one decision per Revision. Returns (record, created)."""
    row = session.get(PlanRevision, (plan_id, revision))
    if row is None:
        raise ApprovalError("PLAN_NOT_FOUND")
    if row.migration_id not in approver.migration_ids:
        raise ApprovalError("APPROVER_FORBIDDEN")
    if revision != latest_revision(session, plan_id):
        raise ApprovalError("STALE_REVISION")
    manifest, preview = _models(row)
    if decision == "APPROVE":
        issues = approval_issues(manifest, preview)
        if issues:
            raise ApprovalError("PLAN_NOT_APPROVABLE", sorted({i.code for i in issues}))
        stored = approval_target(manifest, preview)
    else:
        stored = _stored_identity(manifest, preview)
    requested = ApprovalTarget(
        plan_id=plan_id,
        revision=revision,
        scope=scope,
        manifest_digest=manifest_digest,
        preview_digest=preview_digest,
    )
    if requested != stored:
        raise ApprovalError("APPROVAL_TARGET_MISMATCH")

    existing = _existing_decision(session, plan_id, revision, decision)
    if existing is not None:
        return existing, False
    record = PlanApproval(
        id=str(uuid.uuid4()),
        plan_id=plan_id,
        revision=revision,
        decision=decision,
        approver_id=approver.id,
        scope=stored.scope,
        manifest_digest=stored.manifest_digest,
        preview_digest=stored.preview_digest,
        decided_at=_now(),
        delivery_status="PENDING",
        delivery_attempts=0,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request recorded this Revision first; report that record once.
        session.rollback()
        existing = _existing_decision(session, plan_id, revision, decision)
        if existing is None:
            raise
        return existing, False
    return record, True


def get_approval(session: Session, *, plan_id: str, revision: int, approval_id: str):
    record = session.get(PlanApproval, approval_id)
    if record is None or record.plan_id != plan_id or record.revision != revision:
        raise ApprovalError("APPROVAL_NOT_FOUND")
    return record


def authorize_view(session: Session, approver: Approver, record: PlanApproval) -> None:
    row = session.get(PlanRevision, (record.plan_id, record.revision))
    if row is None or row.migration_id not in approver.migration_ids:
        raise ApprovalError("APPROVER_FORBIDDEN")


def mark_delivery(session: Session, record: PlanApproval, *, error: str | None) -> None:
    record.delivery_attempts += 1
    record.delivery_status = "FAILED" if error else "DELIVERED"
    record.delivery_error = error
    session.commit()


def execution_status(session: Session, record: PlanApproval) -> str:
    marker = session.get(PlanExecution, (record.plan_id, record.revision, EXECUTION_STEP))
    return marker.status if marker else "NOT_STARTED"


Executor = Callable[[dict], None]


def record_only_executor(request: dict) -> None:
    """Safe default: performs no Terraform, Helm or Agent action. The marker is the record."""


def execute_approved(
    session: Session, approval_id: str, executor: Executor, *, now: datetime | None = None
) -> dict:
    """Re-check the DB approval, latest Revision and Digest, then run the executor at most once.

    Returns only small codes. BLOCKED outcomes create no execution marker.
    """
    record = session.get(PlanApproval, approval_id)
    if record is None:
        return {"status": "BLOCKED", "code": "APPROVAL_NOT_FOUND"}
    outcome = {"plan_id": record.plan_id, "revision": record.revision, "approval_id": record.id}
    if record.decision == "REJECT":
        return {**outcome, "status": "REJECTED", "code": "PLAN_REJECTED"}
    marker = session.get(PlanExecution, (record.plan_id, record.revision, EXECUTION_STEP))
    if marker is not None:
        return {**outcome, "status": "ALREADY_EXECUTED", "code": f"EXECUTION_{marker.status}"}
    if record.revision != latest_revision(session, record.plan_id):
        return {**outcome, "status": "BLOCKED", "code": "STALE_REVISION"}
    row = session.get(PlanRevision, (record.plan_id, record.revision))
    manifest, preview = _models(row)
    recorded = ApprovalTarget(
        plan_id=record.plan_id,
        revision=record.revision,
        scope=record.scope,
        manifest_digest=record.manifest_digest,
        preview_digest=record.preview_digest,
    )
    try:
        verify_approval_target(manifest, preview, recorded, requested_scope=record.scope)
    except ProvisioningInputError as exc:
        return {**outcome, "status": "BLOCKED", "code": exc.issues[0].code}

    session.add(
        PlanExecution(
            plan_id=record.plan_id,
            revision=record.revision,
            step=EXECUTION_STEP,
            approval_id=record.id,
            status="RUNNING",
            started_at=_now(now),
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return {**outcome, "status": "ALREADY_EXECUTED", "code": "EXECUTION_RUNNING"}
    marker = session.get(PlanExecution, (record.plan_id, record.revision, EXECUTION_STEP))
    try:
        executor(
            {
                "plan_id": record.plan_id,
                "revision": record.revision,
                "scope": record.scope,
                "manifest_digest": record.manifest_digest,
                "approval_id": record.id,
            }
        )
    except Exception:  # noqa: BLE001 - boundary: record, never hide or re-raise
        # Never re-issue automatically: a real executor may have partially applied changes.
        marker.status = "FAILED"
        marker.finished_at = _now(now)
        session.commit()
        return {**outcome, "status": "FAILED", "code": "EXECUTOR_FAILED"}
    marker.status = "SUCCEEDED"
    marker.finished_at = _now(now)
    session.commit()
    return {**outcome, "status": "EXECUTED", "code": "EXECUTION_SUCCEEDED"}
