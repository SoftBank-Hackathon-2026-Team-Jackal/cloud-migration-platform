"""Business-DB claim/completion around provider execution and the Migration handoff (07/08/13)."""

import asyncio
from collections.abc import Callable

from cmp_api.approvals import claim_execution, complete_execution
from cmp_api.provisioning_results import load_result
from cmp_api.tunnels import tunnel_view
from cmp_providers.provisioning_result import handoff
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity


class ApprovalActivities:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Callable[[], int] | None = None,
    ):
        self._session_factory = session_factory
        # Ticket 25 F4: tunnel freshness is judged at read time; tests inject the clock.
        self._clock = clock

    def _claim(self, approval_id: str, claimant: str) -> dict:
        with self._session_factory() as session:
            return claim_execution(session, approval_id, claimant=claimant)

    def _complete(self, result: dict) -> dict:
        with self._session_factory() as session:
            return complete_execution(
                session,
                result["approval_id"],
                status=result["status"],
                code=result["code"],
                resource_refs=result.get("resource_refs", {}),
                step=result.get("step"),
            )

    def _handoff(self, request: dict) -> dict:
        plan_id, revision = request["plan_id"], request["revision"]
        consumer = request["consumer"]
        if consumer not in ("DATA_MIGRATION", "TRAFFIC_MIGRATION"):
            raise ValueError("unsupported handoff consumer")
        with self._session_factory() as session:
            result = load_result(session, plan_id, revision)
            tunnel = None
            if result is not None and result.provider == "ON_PREM":
                now = self._clock() if self._clock else None
                tunnel = tunnel_view(session, result.cluster.agent_id, now=now)
            return handoff(result, consumer, plan_id, revision, tunnel)

    @activity.defn(name="claim_approved_execution")
    async def claim_approved_execution(self, approval_id: str) -> dict:
        info = activity.info()  # ticket 49 F10: a retried claim of this run is not a second run
        claimant = f"{info.workflow_id}/{info.workflow_run_id}"
        return await asyncio.to_thread(self._claim, approval_id, claimant)

    @activity.defn(name="complete_approved_execution")
    async def complete_approved_execution(self, result: dict) -> dict:
        return await asyncio.to_thread(self._complete, result)

    @activity.defn(name="get_migration_handoff")
    async def get_migration_handoff(self, request: dict) -> dict:
        """Ticket 13 R7: Data/Traffic Migration read the stored result by Plan Revision."""
        return await asyncio.to_thread(self._handoff, request)
