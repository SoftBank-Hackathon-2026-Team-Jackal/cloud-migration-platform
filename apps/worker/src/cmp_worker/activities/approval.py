"""Business-DB claim and completion around provider execution Activities (tickets 07/08)."""

import asyncio

from cmp_api.approvals import claim_execution, complete_execution
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity


class ApprovalActivities:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def _claim(self, approval_id: str) -> dict:
        with self._session_factory() as session:
            return claim_execution(session, approval_id)

    def _complete(self, result: dict) -> dict:
        with self._session_factory() as session:
            return complete_execution(
                session,
                result["approval_id"],
                status=result["status"],
                code=result["code"],
                resource_refs=result.get("resource_refs", {}),
            )

    @activity.defn(name="claim_approved_execution")
    async def claim_approved_execution(self, approval_id: str) -> dict:
        return await asyncio.to_thread(self._claim, approval_id)

    @activity.defn(name="complete_approved_execution")
    async def complete_approved_execution(self, result: dict) -> dict:
        return await asyncio.to_thread(self._complete, result)
