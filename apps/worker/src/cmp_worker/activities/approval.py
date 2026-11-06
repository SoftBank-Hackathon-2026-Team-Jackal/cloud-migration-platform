"""Business-DB claim/completion around provider execution and the Migration handoff (07/08/13)."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from cmp_api.approvals import claim_execution, complete_execution
from cmp_api.config import settings
from cmp_api.provisioning_results import load_result
from cmp_api.tunnels import tunnel_view
from cmp_contracts.handoff import (
    TRAFFIC_HANDOVER_WAIT_MAX_S,
    TrafficHandoff,
    TrafficHandoverRequest,
)
from cmp_providers.provisioning_result import (
    after_rollout,
    handoff,
    traffic_handover,
    tunnel_wait_needed,
)
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity

# Ticket 64: the `provision.check` shown while the lookup waits for the tunnel health check.
TUNNEL_CHECK = "tunnel_app_health"


class ApprovalActivities:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Callable[[], int] | None = None,
        *,
        timer: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wait_seconds: float | None = None,
        poll_seconds: float | None = None,
    ):
        self._session_factory = session_factory
        # Ticket 25 F4: tunnel freshness is judged at read time; tests inject the clock.
        self._clock = clock
        # Ticket 64: the ON_PREM handover wait (deadline timer, sleep, cap, read interval).
        self._timer, self._sleep = timer, sleep
        wait = settings.traffic_handover_wait_seconds if wait_seconds is None else wait_seconds
        self.wait_seconds = max(0, min(wait, TRAFFIC_HANDOVER_WAIT_MAX_S))
        poll = settings.traffic_handover_poll_seconds if poll_seconds is None else poll_seconds
        self.poll_seconds = max(0.1, poll)

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

    def _read(self, plan_id: str, revision: int, consumer: str):
        if consumer not in ("DATA_MIGRATION", "TRAFFIC_MIGRATION"):
            raise ValueError("unsupported handoff consumer")
        with self._session_factory() as session:
            result = load_result(session, plan_id, revision)
            tunnel = None
            if result is not None and result.provider == "ON_PREM":
                now = self._clock() if self._clock else None
                tunnel = tunnel_view(session, result.cluster.agent_id, now=now)
            return result, tunnel, handoff(result, consumer, plan_id, revision, tunnel)

    def _handoff(self, request: dict) -> dict:
        return self._read(request["plan_id"], request["revision"], request["consumer"])[2]

    def _traffic(self, plan_id: str, revision: int) -> tuple[TrafficHandoff, str | None]:
        """Ticket 50 view as `TrafficHandoff`; ON_PREM proceeds only on a post-rollout report."""
        result, tunnel, view = self._read(plan_id, revision, "TRAFFIC_MIGRATION")
        converted = traffic_handover(view)
        if result is None:
            return converted, None
        recorded_at = int(result.finished_at.timestamp())
        return after_rollout(converted, tunnel, recorded_at), result.migration_id

    async def _tunnel_check(self, migration_id: str | None, state: str, plan: str) -> None:
        if not (migration_id and activity.in_activity()):
            return
        from cmp_worker.events import emit_event_async  # Worker-side only (DB emitter)

        await emit_event_async(
            migration_id,
            "provision.check",
            "traffic",
            "failed" if state == "failed" else "running",
            {"name": TUNNEL_CHECK, "state": state},
            idempotency_key=f"chk:{TUNNEL_CHECK}:{plan}:{state}",
        )

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

    @activity.defn(name="get_traffic_handover")
    async def get_traffic_handover(self, request: dict) -> dict:
        """Ticket 50: the TRAFFIC_MIGRATION view as the Traffic `Handover` shape (#128 input).

        Ticket 64: an ON_PREM view blocked only by tunnel evidence that is not there yet
        (`tunnel_wait_needed`) is read again every `poll_seconds` until a report from after the
        rollout passes the app health check, another block appears, or `wait_seconds` pass. The
        last view is returned as it is, so a timeout keeps the original `blocked_by` code.
        """
        parsed = TrafficHandoverRequest.model_validate(request)
        plan = f"{parsed.plan_id}:{parsed.revision}"
        converted, migration_id = await asyncio.to_thread(
            self._traffic, parsed.plan_id, parsed.revision
        )
        if not tunnel_wait_needed(converted):
            return converted.model_dump(mode="json")
        await self._tunnel_check(migration_id, "running", plan)
        deadline = self._timer() + self.wait_seconds
        while tunnel_wait_needed(converted):
            remaining = deadline - self._timer()
            if remaining <= 0:
                break
            if activity.in_activity():
                activity.heartbeat(TUNNEL_CHECK)
            await self._sleep(min(self.poll_seconds, remaining))
            converted, _ = await asyncio.to_thread(self._traffic, parsed.plan_id, parsed.revision)
        await self._tunnel_check(migration_id, "done" if converted.proceed else "failed", plan)
        return converted.model_dump(mode="json")
