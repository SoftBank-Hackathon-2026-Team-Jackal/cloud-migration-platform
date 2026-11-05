"""Planning input registration and target settings for Plan publication (ticket 18, P1).

Reference-only: no credential, Secret value or signed URL fits these models. AWS/GCP settings
are the reviewed 05/06 Plan inputs without `migration_id`/`plan`, which Planning assigns.
"""

import hashlib
from typing import Annotated, Literal

from pydantic import Field

from cmp_contracts.chart import KubernetesName
from cmp_contracts.env_plan import EnvPlan
from cmp_contracts.provisioning import Contract, Text
from cmp_contracts.provisioning_v2 import ImageUri, PlanningRequestV2

PLAN_PATCH = "provisioning-plan-v1"
WORKLOAD_PATCH = "provisioning-cloud-workloads-v1"  # ticket 22 W8
WORKLOAD_TEMPLATE = "cloud-workloads-v1"
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 7 * 24 * 3600  # P8: default only, not a product policy.


def plan_id_for(migration_id: str) -> str:
    """P2: one Plan per Migration; a safe opaque ID that satisfies the 05/06 pattern."""
    return "plan-" + hashlib.sha256(migration_id.encode("utf-8")).hexdigest()[:24]


def workload_plan_id_for(migration_id: str) -> str:
    """Ticket 22 W2: the Migration's AWS/GCP WORKLOADS Plan, separate from its infra Plan."""
    digest = hashlib.sha256(f"WORKLOADS:{migration_id}".encode()).hexdigest()
    return "plan-" + digest[:24]


class CloudWorkloadSettings(Contract):
    """Ticket 22 W3: deployment choices for the 09/11 DB + App workloads on the new cluster."""

    namespace: KubernetesName
    storage_class: KubernetesName  # 09/11 create it with their provisioner when absent.
    database_image: ImageUri  # PostgreSQL 16 image pinned by Digest.


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
    workloads: CloudWorkloadSettings | None = None  # ticket 22; absent → no workload Plan


class GcpTargetSettings(Contract):
    provider: Literal["GCP"]
    settings: dict  # GcpPlanInput fields except migration_id/plan.
    workloads: CloudWorkloadSettings | None = None  # ticket 22


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
    # ENV-04 (#99): the approved per-key `.env` plan (key names and dispositions only, never a
    # value). Supplied by the operator now, by analysis later (ENV-05). Its `env_file_ref` must
    # equal `planning_request.environment.env_file_ref`. Absent: production keeps the
    # reference-only behavior (pre-created ConfigMap/Secret). Omitted while unset so stored
    # registrations and their Digests stay identical.
    env_plan: EnvPlan | None = Field(default=None, exclude_if=lambda value: value is None)
