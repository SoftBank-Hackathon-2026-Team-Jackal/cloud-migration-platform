"""`execute_on_prem_plan`: provider step of the approved execution dispatch owned by ticket 08.

08's Workflow calls this after `claim_approved_execution` returns CLAIMED, with a long
start-to-close timeout, a heartbeat timeout and maximum_attempts=1. This Activity re-reads the
Plan Revision, verifies the artifact bytes it uses, queues at most one Agent job per Revision and
heartbeats while waiting. An offline Agent leaves the job QUEUED (state preserved) and returns
UNKNOWN instead of cancelling or re-issuing work (ticket 12, E1/E6).
"""

import asyncio
import time

from cmp_api.agent_registry import AgentJob
from cmp_api.onprem_rollout import (
    ArtifactStore,
    RolloutError,
    build_rollout_job,
    dispatch,
    outcome,
)
from cmp_contracts.onprem_rollout import rollout_job_id
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity


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

    def _dispatch(self, request: dict) -> str:
        with self._session_factory() as session:
            job, owner_id = build_rollout_job(session, request, self._store)
            dispatch(session, job, owner_id)
            return job.job_id

    def _observe(self, job_id: str) -> tuple[str, dict | None]:
        with self._session_factory() as session:
            job = session.get(AgentJob, job_id)
            return job.status, outcome(job)

    @activity.defn(name="execute_on_prem_plan")
    async def execute_on_prem_plan(self, request: dict) -> dict:
        job_id = rollout_job_id(request["plan_id"], request["revision"])
        try:
            job_id = await asyncio.to_thread(self._dispatch, request)
        except RolloutError as exc:
            return {"status": "FAILED", "code": exc.code, "resource_refs": {"job_id": job_id}}
        deadline = time.monotonic() + self._wait
        while True:
            status, result = await asyncio.to_thread(self._observe, job_id)
            if result is not None:
                return result
            activity.heartbeat({"job_id": job_id, "agent_job_status": status})
            if time.monotonic() >= deadline:
                code = "AGENT_OFFLINE" if status == "QUEUED" else "AGENT_RESULT_PENDING"
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
