"""Register a prepared Planning input where Planning reads it (ticket 15).

Runs next to `publish_migration_plan` and uses the same database and platform ArtifactStore, so
the stored registration and the requirements bytes are exactly what Planning will validate. The
input and output hold references and codes only.
"""

import asyncio

from cmp_api.onprem_rollout import LocalArtifactStore, RolloutError
from cmp_api.plan_inputs import PlanInputError, register_planning_input
from cmp_contracts.plan_publish import PlanningInputRegistration
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity


class PlanningRegisterActivities:
    def __init__(self, session_factory: sessionmaker[Session], store: LocalArtifactStore | None):
        self._sessions = session_factory
        self._store = store

    @activity.defn(name="register_planning_input")
    async def register_planning_input(self, payload: dict) -> dict:
        if self._store is None:
            return {"status": "SCAFFOLD", "code": "PLANNING_NOT_CONFIGURED"}
        return await asyncio.to_thread(self._register, payload)

    def _register(self, payload: dict) -> dict:
        try:
            data = self._store.read_raw(payload["registration_ref"]["sha256"])
            registration = PlanningInputRegistration.model_validate_json(data)
        except (RolloutError, ValidationError):
            return {"status": "REFUSED", "code": "REGISTRATION_UNAVAILABLE"}
        with self._sessions() as session:
            try:
                done = register_planning_input(
                    session, payload["migration_id"], registration, self._store
                )
            except PlanInputError as exc:
                return {"status": "REFUSED", "code": exc.code}
        return {"status": "REGISTERED", "input_digest": done["input_digest"]}


def build_planning_register_activities(session_factory) -> PlanningRegisterActivities:
    from pathlib import Path

    from cmp_api.config import settings as api_settings

    if not api_settings.artifact_store_dir:
        return PlanningRegisterActivities(session_factory, None)
    return PlanningRegisterActivities(
        session_factory, LocalArtifactStore(Path(api_settings.artifact_store_dir))
    )
