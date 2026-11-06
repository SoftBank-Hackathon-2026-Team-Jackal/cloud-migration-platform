"""Ticket 48 (#183) G7: GCP_TARGET connection check delegated to the provisioning worker.

The API holds no GCP credentials (ticket 41 D1). It runs `GcpConnectionCheckWorkflow` and waits
for the worker Activity's result. Only the expected principal email and a stable code cross
Temporal; Temporal or worker unavailability is `GCP_CHECK_UNAVAILABLE`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

from cmp_contracts.queues import GCP_CONNECTION_CHECK_WORKFLOW_NAME, WORKFLOW_QUEUE

from cmp_api.connections import ConnectionFailure
from cmp_api.temporal_client import get_temporal_client

GCP_CHECK_CODES = frozenset(
    {"ADC_PRINCIPAL_MISMATCH", "ADC_UNAVAILABLE", "ADC_CHECK_FAILED", "ADC_SOURCE_UNSUPPORTED"}
)


class TemporalGcpChecker:
    def __init__(
        self,
        client_factory: Callable[[], Awaitable] = get_temporal_client,
        *,
        timeout_seconds: float = 45,
    ):
        self._client_factory = client_factory
        self._timeout = timeout_seconds

    def __call__(self, expected_principal: str) -> str:
        # Sync FastAPI routes run in a worker thread without an event loop.
        return asyncio.run(self.check(expected_principal))

    async def check(self, expected_principal: str) -> str:
        try:
            client = await self._client_factory()
            result = await asyncio.wait_for(
                client.execute_workflow(
                    GCP_CONNECTION_CHECK_WORKFLOW_NAME,
                    expected_principal,
                    id=f"gcp-connection-check-{uuid4().hex}",
                    task_queue=WORKFLOW_QUEUE,
                    execution_timeout=timedelta(seconds=self._timeout),
                ),
                # Grace past the Workflow's own execution timeout for the RPC round trip.
                timeout=self._timeout + min(5.0, self._timeout),
            )
        except Exception:  # noqa: BLE001 - any Temporal/worker failure is one stable code
            raise ConnectionFailure("GCP_CHECK_UNAVAILABLE") from None
        if not isinstance(result, dict):
            raise ConnectionFailure("GCP_CHECK_FAILED")
        code = result.get("code")
        if code == "OK" and result.get("principal") == expected_principal:
            return expected_principal
        raise ConnectionFailure(code if code in GCP_CHECK_CODES else "GCP_CHECK_FAILED")
