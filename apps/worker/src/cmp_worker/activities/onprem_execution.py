"""`execute_on_prem_plan`: provider step of the approved execution dispatch owned by ticket 08.

08's Workflow calls this after `claim_approved_execution` returns CLAIMED, with a long
start-to-close timeout, a heartbeat timeout and maximum_attempts=1. This Activity re-reads the
Plan Revision, verifies the artifact bytes it uses, queues at most one Agent job per Revision and
heartbeats while waiting. The job may only be started until this wait ends (ticket 52, D2/D3):
if the Agent has not claimed it by then it is closed as EXPIRED and the result is
`FAILED AGENT_OFFLINE` (nothing ran), so a returning Agent never deploys untracked work. A job
the Agent already runs is left alone and reported as `UNKNOWN AGENT_RESULT_PENDING`.
"""

import asyncio
import time
from datetime import UTC, datetime

from cmp_api.agent_jobs import expire_if_queued
from cmp_api.agent_registry import AgentJob
from cmp_api.approvals import PlanRevision
from cmp_api.onprem_rollout import (
    ArtifactStore,
    RolloutError,
    build_rollout_job,
    dispatch,
    outcome,
    values_refs,
)
from cmp_contracts.onprem_rollout import rollout_job_id
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity

from cmp_worker.events import log_event
from cmp_worker.provisioning_events import (
    OnPremRolloutEvents,
    dispatch_failed_resource,
    onprem_resources,
)


class OnPremExecutionActivities:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        store: ArtifactStore,
        *,
        wait_seconds: float = 1800,
        poll_seconds: float = 5,
    ):
        self._session_factory = session_factory
        self._store = store
        self._wait = wait_seconds
        self._poll = poll_seconds

    def _dispatch(self, request: dict) -> OnPremRolloutEvents | None:
        """Queue (or reuse) the Revision's job; returns its web events (None: no Migration)."""
        deadline = int(datetime.now(UTC).timestamp() + self._wait)
        with self._session_factory() as session:
            job, owner_id = build_rollout_job(session, request, self._store)
            dispatch(session, job, owner_id, deadline_at=deadline)
            migration_id = self._migration_id(session, request)
            if migration_id is None:
                return None
            resources = onprem_resources(job.model_dump(mode="json"))
            return OnPremRolloutEvents(migration_id, job.job_id, tuple(resources))

    @staticmethod
    def _migration_id(session: Session, request: dict) -> str | None:
        row = session.get(PlanRevision, (request["plan_id"], request["revision"]))
        return None if row is None else row.migration_id

    def _rejected_events(self, request: dict, job_id: str) -> OnPremRolloutEvents | None:
        with self._session_factory() as session:
            migration_id = self._migration_id(session, request)
        if migration_id is None:
            return None
        return OnPremRolloutEvents(migration_id, job_id, tuple(dispatch_failed_resource(job_id)))

    def _expire(self, job_id: str) -> None:
        with self._session_factory() as session:
            expire_if_queued(session, job_id, "AGENT_OFFLINE")

    def _observe(self, job_id: str) -> tuple[str, dict | None]:
        with self._session_factory() as session:
            job = session.get(AgentJob, job_id)
            result = outcome(job)
            if result is not None and result["status"] == "SUCCEEDED":
                payload = job.payload
                extra = values_refs(session, self._store, payload["plan_id"], payload["revision"])
                result["resource_refs"] = {**result["resource_refs"], **extra}
            return job.status, result

    @activity.defn(name="execute_on_prem_plan")
    async def execute_on_prem_plan(self, request: dict) -> dict:
        job_id = rollout_job_id(request["plan_id"], request["revision"])
        try:
            events = await asyncio.to_thread(self._dispatch, request)
        except RolloutError as exc:
            result = {"status": "FAILED", "code": exc.code, "resource_refs": {"job_id": job_id}}
            await self._events(lambda: self._rejected_events(request, job_id), result)
            return result
        await self._events(lambda: events, None)
        result = await self._wait_result(job_id)
        await self._events(lambda: events, result)
        return result

    async def _events(self, events_of, result: dict | None) -> None:
        """Web events (ticket 63). Never changes the Activity result: failures only log."""

        def emit() -> None:
            events = events_of()
            if events is None:
                return
            try:
                if result is None:
                    events.started()
                else:
                    events.finished(result)
            except Exception as error:  # noqa: BLE001 — events never fail the rollout
                log_event(
                    events.migration_id,
                    "warning",
                    "PROVISION_EVENTS_UNAVAILABLE",
                    {"error": type(error).__name__},
                )

        try:
            await asyncio.to_thread(emit)
        except Exception:  # noqa: BLE001 — e.g. the Revision could not be read for its Migration
            return

    async def _wait_result(self, job_id: str) -> dict:
        deadline = time.monotonic() + self._wait
        while True:
            status, result = await asyncio.to_thread(self._observe, job_id)
            if result is not None:
                return result
            activity.heartbeat({"job_id": job_id, "agent_job_status": status})
            if time.monotonic() >= deadline:
                if status == "QUEUED":
                    await asyncio.to_thread(self._expire, job_id)
                    status, result = await asyncio.to_thread(self._observe, job_id)
                    if result is not None:  # EXPIRED, or the Agent finished meanwhile
                        return result
                code = "AGENT_RESULT_PENDING"
                return {"status": "UNKNOWN", "code": code, "resource_refs": {"job_id": job_id}}
            await asyncio.sleep(self._poll)


def execute_on_prem_plan_activity():
    """Worker registration: business DB + configured artifact store (unset → ARTIFACT_UNAVAILABLE)."""
    from pathlib import Path

    from cmp_api.config import settings
    from cmp_api.db import SessionLocal
    from cmp_api.onprem_rollout import LocalArtifactStore

    root = Path(settings.artifact_store_dir or ".cmp-artifacts-unconfigured")
    return OnPremExecutionActivities(SessionLocal, LocalArtifactStore(root)).execute_on_prem_plan
