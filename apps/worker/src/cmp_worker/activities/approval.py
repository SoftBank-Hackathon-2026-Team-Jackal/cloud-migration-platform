"""Business-DB re-check immediately before the (fake) executor. No cloud execution."""

import asyncio

from cmp_api.approvals import Executor, execute_approved, record_only_executor
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity


class ApprovalActivities:
    def __init__(
        self, session_factory: sessionmaker[Session], executor: Executor = record_only_executor
    ):
        self._session_factory = session_factory
        self._executor = executor

    def _execute(self, approval_id: str) -> dict:
        with self._session_factory() as session:
            return execute_approved(session, approval_id, self._executor)

    @activity.defn(name="execute_approved_plan")
    async def execute_approved_plan(self, approval_id: str) -> dict:
        return await asyncio.to_thread(self._execute, approval_id)
