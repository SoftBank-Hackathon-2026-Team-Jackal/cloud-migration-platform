"""Business-DB storage of ProvisioningResult v1 (ticket 13, decision R3).

One immutable row per Plan Revision, written in the same transaction that finishes the
execution marker. Downstream Workers read it through `load_result`; nothing here performs
cloud or cluster I/O.
"""

import json
import logging
from datetime import UTC, datetime

from cmp_contracts.plan import PlanManifest
from cmp_contracts.provisioning_result import ProvisioningResult
from cmp_providers.provisioning_result import build_provisioning_result
from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from cmp_api.db import Base

logger = logging.getLogger(__name__)


class ProvisioningResultRecord(Base):
    __tablename__ = "provisioning_results"

    plan_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    migration_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


def add_result(
    session: Session,
    manifest: dict,
    *,
    approval_id: str,
    status: str,
    code: str,
    step: str | None,
    refs: dict[str, str],
    finished_at: int,
) -> None:
    """Stage the result row; the caller commits with the execution marker. Never overwrites."""
    plan = PlanManifest.model_validate(manifest)
    if session.get(ProvisioningResultRecord, (plan.plan.plan_id, plan.plan.revision)):
        return
    try:
        result = build_provisioning_result(
            plan,
            approval_id=approval_id,
            status=status,
            code=code,
            step=step,
            refs=refs,
            finished_at=datetime.fromtimestamp(finished_at, UTC),
        )
    except ValueError:
        # Never block the execution marker; a missing result keeps the handoff closed
        # (RESULT_NOT_FOUND) instead of guessing.
        logger.warning("provisioning result not built", extra={"code": "RESULT_BUILD_FAILED"})
        return
    session.add(
        ProvisioningResultRecord(
            plan_id=result.plan_id,
            revision=result.revision,
            migration_id=result.migration_id,
            status=result.status,
            result=result.model_dump(mode="json"),
            created_at=finished_at,
        )
    )


def load_result(session: Session, plan_id: str, revision: int) -> ProvisioningResult | None:
    row = session.get(ProvisioningResultRecord, (plan_id, revision))
    if row is None:
        return None
    return ProvisioningResult.model_validate_json(json.dumps(row.result))
