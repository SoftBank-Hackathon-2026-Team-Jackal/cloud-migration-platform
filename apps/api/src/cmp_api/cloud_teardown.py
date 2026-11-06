"""Ticket 53 (D1, D11): which executed Plan Revisions an operator may tear down.

Only an INFRASTRUCTURE/FULL AWS/GCP Revision with a finished execution marker qualifies, and not
while any execution of the same target is still RUNNING. The Worker re-checks the same rules
(`cmp_worker.activities.cloud_teardown.load_context`).
"""

from cmp_contracts.plan import PlanManifest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cmp_api.approvals import EXECUTION_STEP, PlanExecution, PlanRevision

TEARDOWN_PREFIX = "cloud-teardown-"


class TeardownError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def teardown_id(plan_id: str, revision: int) -> str:
    return f"{TEARDOWN_PREFIX}{plan_id}-r{revision}"


def check_teardown_target(session: Session, plan_id: str, revision: int) -> PlanManifest:
    row = session.get(PlanRevision, (plan_id, revision))
    if row is None:
        raise TeardownError("PLAN_NOT_FOUND")
    manifest = PlanManifest.model_validate(row.manifest)
    if manifest.scope not in ("INFRASTRUCTURE", "FULL") or manifest.target.provider not in (
        "AWS",
        "GCP",
    ):
        raise TeardownError("UNSUPPORTED_EXECUTION_SCOPE")
    if session.get(PlanExecution, (plan_id, revision, EXECUTION_STEP)) is None:
        raise TeardownError("NOT_EXECUTED")
    running = session.execute(
        select(PlanRevision)
        .join(
            PlanExecution,
            (PlanExecution.plan_id == PlanRevision.plan_id)
            & (PlanExecution.revision == PlanRevision.revision),
        )
        .where(PlanExecution.status == "RUNNING")
    ).scalars()
    if any(PlanManifest.model_validate(r.manifest).target == manifest.target for r in running):
        raise TeardownError("EXECUTION_IN_PROGRESS")
    return manifest
