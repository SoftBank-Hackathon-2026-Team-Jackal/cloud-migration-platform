"""Ticket 48 (#183) G7: GCP_TARGET connection check where the GCP identity lives.

The demo key (or WIF credential config) is mounted into the worker only, so the API delegates
the ADC principal check here through `GcpConnectionCheckWorkflow`. The result carries a stable
code and the verified principal email only; tokens and Google messages never leave this Activity.
"""

import asyncio

from cmp_contracts.provisioning import ProvisioningInputError
from cmp_contracts.queues import GCP_CONNECTION_CHECK_ACTIVITY
from cmp_providers.gcp_preview import verify_adc_principal
from temporalio import activity


@activity.defn(name=GCP_CONNECTION_CHECK_ACTIVITY)
async def verify_gcp_connection(expected_principal: str) -> dict:
    def check() -> dict:
        try:
            principal = verify_adc_principal(expected_principal)
        except ProvisioningInputError as exc:
            return {"code": exc.issues[0].code, "principal": None}
        return {"code": "OK", "principal": principal}

    return await asyncio.to_thread(check)
