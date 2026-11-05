"""Planning input registration and target settings for Plan publication (ticket 18, P1).

Reference-only: no credential, Secret value or signed URL fits these models. AWS/GCP settings
are the reviewed 05/06 Plan inputs without `migration_id`/`plan`, which Planning assigns.
"""

import hashlib
from typing import Annotated, Literal

from pydantic import Field

from cmp_contracts.chart import KubernetesName
from cmp_contracts.provisioning import Contract, Text
from cmp_contracts.provisioning_v2 import ImageUri, PlanningRequestV2

PLAN_PATCH = "provisioning-plan-v1"
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 7 * 24 * 3600  # P8: default only, not a product policy.


def plan_id_for(migration_id: str) -> str:
    """P2: one Plan per Migration; a safe opaque ID that satisfies the 05/06 pattern."""
    return "plan-" + hashlib.sha256(migration_id.encode("utf-8")).hexdigest()[:24]


class OnPremTargetSettings(Contract):
    provider: Literal["ON_PREM"]
    environment_id: Text
    agent_id: Text
    cluster_uid: Text  # Must equal the UID the Agent registered (P4).
    namespace: KubernetesName
    storage_class: KubernetesName
    database_image: ImageUri  # PostgreSQL 16 image pinned by Digest.


class AwsTargetSettings(Contract):
    provider: Literal["AWS"]
    # AwsPlanInput fields except migration_id/plan; validated when the Plan is built.
    settings: dict


class GcpTargetSettings(Contract):
    provider: Literal["GCP"]
    settings: dict  # GcpPlanInput fields except migration_id/plan.


TargetSettings = Annotated[
    OnPremTargetSettings | AwsTargetSettings | GcpTargetSettings,
    Field(discriminator="provider"),
]


class PlanningInputRegistration(Contract):
    contract_version: Literal["1"]
    planning_request: PlanningRequestV2
    target: TargetSettings
    # Optional exact requirements bytes (UTF-8 text) when the store does not hold them yet.
    requirements_json: str | None = None
