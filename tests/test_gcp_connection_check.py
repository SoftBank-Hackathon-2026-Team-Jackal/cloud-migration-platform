"""Ticket 48 (#183) G7: the GCP connection check runs in the provisioning worker, not the API.

Seam: the API checker (`TemporalGcpChecker`) against a Temporal test server running the real
workflow and provisioning workers; the ADC check itself is replaced at the Activity boundary
(its rules are covered by test_gcp_adc.py).
"""

import asyncio
import os

import pytest
from cmp_api.connections import ConnectionFailure
from cmp_api.gcp_connection_check import TemporalGcpChecker
from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue
from cmp_worker.activities import gcp_connection
from cmp_worker.workers.migration import create_workflow_worker
from cmp_worker.workers.provisioning import create_worker as provisioning_worker
from temporalio.testing import WorkflowEnvironment

SA = "cmp-provisioning@sbh26-prod.iam.gserviceaccount.com"


def fake_adc(monkeypatch, seen):
    def verify(expected):
        seen.append(expected)
        if expected != SA:
            raise ProvisioningInputError(
                [ValidationIssue(code="ADC_PRINCIPAL_MISMATCH", path="adc", message="x")]
            )
        return expected

    monkeypatch.setattr(gcp_connection, "verify_adc_principal", verify)


@pytest.mark.asyncio
async def test_worker_activity_decides_the_gcp_connection_result(monkeypatch):
    seen = []
    fake_adc(monkeypatch, seen)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        provisioning_worker(env.client),
        create_workflow_worker(env.client),
    ):

        async def client():
            return env.client

        checker = TemporalGcpChecker(client)
        assert await checker.check(SA) == SA
        with pytest.raises(ConnectionFailure) as failure:
            await checker.check("other@sbh26-prod.iam.gserviceaccount.com")
        assert failure.value.code == "ADC_PRINCIPAL_MISMATCH"
    assert seen == [SA, "other@sbh26-prod.iam.gserviceaccount.com"]


def test_unreachable_temporal_is_a_stable_code():
    async def broken():
        raise RuntimeError("connection refused")

    with pytest.raises(ConnectionFailure) as failure:
        TemporalGcpChecker(broken)(SA)  # the sync form used by the API route
    assert failure.value.code == "GCP_CHECK_UNAVAILABLE"


@pytest.mark.parametrize(
    "result",
    [
        {"code": "OK", "principal": "other@sbh26-prod.iam.gserviceaccount.com"},
        {"code": "SOMETHING_ELSE", "principal": None},
        "not-a-dict",
    ],
)
def test_unexpected_worker_results_fail_closed(result):
    class Client:
        async def execute_workflow(self, *_args, **_kwargs):
            return result

    async def factory():
        return Client()

    with pytest.raises(ConnectionFailure) as failure:
        TemporalGcpChecker(factory)(SA)
    assert failure.value.code == "GCP_CHECK_FAILED"


def test_timeout_is_unavailable():
    class Client:
        async def execute_workflow(self, *_args, **_kwargs):
            await asyncio.sleep(5)

    async def factory():
        return Client()

    with pytest.raises(ConnectionFailure) as failure:
        TemporalGcpChecker(factory, timeout_seconds=0.05)(SA)
    assert failure.value.code == "GCP_CHECK_UNAVAILABLE"


def test_api_default_checker_no_longer_reads_api_adc():
    from cmp_api.routes import connections as routes

    assert isinstance(routes.get_gcp_checker(), TemporalGcpChecker)
