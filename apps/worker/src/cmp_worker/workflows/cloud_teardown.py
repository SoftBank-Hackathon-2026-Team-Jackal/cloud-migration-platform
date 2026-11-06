"""Ticket 53: operator-started teardown of an AWS/GCP target (D1~D5, D9).

workloads → reviewed destroy Plan → wait for the operator's confirmation of the review Digest
(at most `CONFIRM_TIMEOUT`) → destroy. The `status` Query is the only state store (D2). Workflow
code is deterministic; every external call is an Activity on the Provisioning queue.
"""

from datetime import timedelta

from cmp_contracts.queues import PROVISIONING_QUEUE
from temporalio import workflow
from temporalio.common import RetryPolicy

CONFIRM_TIMEOUT = timedelta(hours=24)
WORKLOADS = {
    "task_queue": PROVISIONING_QUEUE,
    "start_to_close_timeout": timedelta(hours=1),
    "heartbeat_timeout": timedelta(minutes=5),
    "retry_policy": RetryPolicy(maximum_attempts=3),  # idempotent (D9)
}
DESTROY_PLAN = {
    "task_queue": PROVISIONING_QUEUE,
    "start_to_close_timeout": timedelta(minutes=40),
    "heartbeat_timeout": timedelta(minutes=5),
    "retry_policy": RetryPolicy(maximum_attempts=2),  # plan only; the State lock is released
}
DESTROY_APPLY = {
    "task_queue": PROVISIONING_QUEUE,
    # 3 Plans + up to two Applies of 90 min, each with a 5 min SIGTERM grace (D8, D9).
    "start_to_close_timeout": timedelta(hours=4),
    "heartbeat_timeout": timedelta(minutes=5),
    "retry_policy": RetryPolicy(maximum_attempts=1),  # never re-run a destroy automatically
}


def teardown_id(plan_id: str, revision: int) -> str:
    return f"cloud-teardown-{plan_id}-r{revision}"


@workflow.defn(name="CloudTeardownWorkflow")
class CloudTeardownWorkflow:
    def __init__(self) -> None:
        self._status: dict = {"phase": "WORKLOADS"}
        self._confirmed: str | None = None

    @workflow.signal
    def confirm_destroy(self, review_digest: str) -> None:
        """Only the Digest of the current review is accepted (the API checks it first)."""
        review = self._status.get("review") or {}
        if self._status.get("phase") == "AWAITING_CONFIRMATION" and review_digest == review.get(
            "digest"
        ):
            self._confirmed = review_digest

    @workflow.query
    def status(self) -> dict:
        return self._status

    def _finish(self, result: dict) -> dict:
        self._status = {**self._status, "phase": "FINISHED", "result": result}
        return result

    @workflow.run
    async def run(self, request: dict) -> dict:
        target = {key: request[key] for key in ("plan_id", "revision")}
        self._status["delete_data"] = bool(request.get("delete_data"))
        workloads = await workflow.execute_activity(
            "teardown_cloud_workloads", {**target, "delete_data": bool(request.get("delete_data"))},
            **WORKLOADS,
        )  # fmt: skip
        self._status["workloads"] = workloads
        if workloads.get("status") != "SUCCEEDED":  # D3: LBs/disks first, or no destroy
            return self._finish(workloads)

        self._status["phase"] = "DESTROY_PLAN"
        planned = await workflow.execute_activity("plan_cloud_destroy", target, **DESTROY_PLAN)
        if planned.get("status") != "SUCCEEDED" or planned.get("code") == "NOTHING_TO_DESTROY":
            return self._finish({k: v for k, v in planned.items() if k != "review"})
        self._status["review"] = planned["review"]
        self._status["phase"] = "AWAITING_CONFIRMATION"
        try:
            await workflow.wait_condition(
                lambda: self._confirmed is not None, timeout=CONFIRM_TIMEOUT
            )
        except TimeoutError:
            return self._finish(
                {"status": "UNKNOWN", "code": "DESTROY_NOT_CONFIRMED", "resource_refs": {}}
            )

        self._status["phase"] = "DESTROYING"
        result = await workflow.execute_activity(
            "apply_cloud_destroy", {**target, "review_digest": self._confirmed}, **DESTROY_APPLY
        )
        return self._finish(result)
