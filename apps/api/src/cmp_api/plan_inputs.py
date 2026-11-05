"""Per-Migration Planning input registration (ticket 18, P1/P3).

Stores the reference-only Planning request v2 and target settings that Planning turns into a
Plan Revision. The input Digest decides whether a re-run reuses the latest Revision.
"""

from datetime import UTC, datetime

from cmp_contracts.plan_publish import PlanningInputRegistration
from cmp_providers.artifacts import sha256_bytes, sha256_json
from cmp_providers.aws_preview import AwsPlanInput
from cmp_providers.gcp_preview import GcpPlanInput
from pydantic import ValidationError
from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from cmp_api.db import Base
from cmp_api.onprem_rollout import LocalArtifactStore, RolloutError

CLOUD_TEMPLATES = {"AWS": "aws-eks-v2", "GCP": "gcp-gke-v2"}
_CLOUD_MODELS = {"AWS": AwsPlanInput, "GCP": GcpPlanInput}


class PlanInputError(ValueError):
    """Code-only error; never echoes input values."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PlanningInput(Base):
    __tablename__ = "provisioning_planning_inputs"

    migration_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    registration: Mapped[dict] = mapped_column(JSON, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_at: Mapped[int] = mapped_column(Integer, nullable=False)


def template_version(registration: PlanningInputRegistration) -> str:
    provider = registration.target.provider
    return CLOUD_TEMPLATES.get(provider) or registration.planning_request.policy.template_version


def cloud_plan_input(registration: PlanningInputRegistration, plan: dict):
    """The reviewed 05/06 Plan input for this registration and Plan identity."""
    target = registration.target
    settings = target.settings
    if "migration_id" in settings or "plan" in settings:
        raise PlanInputError("INVALID_TARGET_SETTINGS")
    try:
        return _CLOUD_MODELS[target.provider].model_validate(
            {
                **settings,
                "migration_id": registration.planning_request.migration_id,
                "plan": plan,
            }
        )
    except ValidationError:
        raise PlanInputError("INVALID_TARGET_SETTINGS") from None


def input_digest(registration: PlanningInputRegistration) -> str:
    request = registration.planning_request
    return sha256_json(
        {
            "contract_version": registration.contract_version,
            "planning_request": request.model_dump(mode="json"),
            "target": registration.target.model_dump(mode="json"),
            "requirements_sha256": request.analysis.requirements_ref.sha256,
            "template_version": template_version(registration),
        }
    )


def _check(registration: PlanningInputRegistration, migration_id: str) -> None:
    request = registration.planning_request
    selection = request.target_selection
    if request.migration_id != migration_id:
        raise PlanInputError("MIGRATION_MISMATCH")
    if registration.target.provider != selection.provider:
        raise PlanInputError("TARGET_PROVIDER_MISMATCH")
    if selection.provider in _CLOUD_MODELS:
        plan = {"plan_id": "validation", "revision": 1, "template_version": "validation"}
        if cloud_plan_input(registration, plan).region != selection.location:
            raise PlanInputError("TARGET_LOCATION_MISMATCH")


def _requirements(registration: PlanningInputRegistration, store: LocalArtifactStore) -> None:
    expected = registration.planning_request.analysis.requirements_ref.sha256
    if registration.requirements_json is not None:
        content = registration.requirements_json.encode("utf-8")
        if sha256_bytes(content) != expected:
            raise PlanInputError("REQUIREMENTS_DIGEST_MISMATCH")
        store.write_raw(expected, content)
        return
    try:
        store.read_raw(expected)
    except RolloutError:
        raise PlanInputError("REQUIREMENTS_UNAVAILABLE") from None


def register_planning_input(
    session: Session,
    migration_id: str,
    registration: PlanningInputRegistration,
    store: LocalArtifactStore,
) -> dict:
    """Validate and store (replace) the Migration's Planning input. Returns its input Digest."""
    registration = PlanningInputRegistration.model_validate(registration.model_dump(mode="json"))
    _check(registration, migration_id)
    _requirements(registration, store)
    stored = registration.model_dump(mode="json", exclude={"requirements_json"})
    digest = input_digest(registration)
    row = session.get(PlanningInput, migration_id)
    now = int(datetime.now(UTC).timestamp())
    if row is None:
        session.add(
            PlanningInput(
                migration_id=migration_id,
                registration=stored,
                input_digest=digest,
                registered_at=now,
            )
        )
    else:
        row.registration, row.input_digest, row.registered_at = stored, digest, now
    session.commit()
    return {"migration_id": migration_id, "input_digest": digest}


def load_planning_input(session: Session, migration_id: str) -> PlanningInputRegistration | None:
    row = session.get(PlanningInput, migration_id)
    return PlanningInputRegistration.model_validate(row.registration) if row else None
