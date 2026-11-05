"""Approved ON_PREM rollout Agent work (ticket 12, E2).

Only identifiers, ArtifactRefs and Secret names/keys cross this boundary. Values/Chart bytes are
downloaded separately and verified by digest; Secret values never appear here.
"""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_contracts.artifact import ArtifactRef, Digest
from cmp_contracts.chart import KubernetesName
from cmp_contracts.provisioning import Contract, Positive, Text
from cmp_contracts.shared_deployment_v2 import SecretRefV2

ReleaseName = Annotated[KubernetesName, Field(max_length=53)]
ROLLOUT_MODE = "PRODUCTION_ROLLOUT"


class RolloutConnectionUrl(Contract):
    """Ticket 19: the Agent builds the URL in the cluster from the password Secret; only the
    target Secret reference and non-secret parts cross this boundary."""

    secret: SecretRefV2
    database: Text
    username: Text


class RolloutDatabase(Contract):
    release: ReleaseName
    chart: ArtifactRef
    values: ArtifactRef
    password_secret: SecretRefV2
    # Omitted when unset so jobs queued before ticket 19 keep identical payloads.
    connection_url: RolloutConnectionUrl | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RolloutApplication(Contract):
    release: ReleaseName
    chart: ArtifactRef
    values: ArtifactRef
    port: int = Field(ge=1, le=65535)
    health_path: Annotated[str, Field(pattern=r"^/[A-Za-z0-9/_.-]*$")]
    # User-provided Secrets that must already exist; checked before any Helm mutation.
    secret_refs: list[SecretRefV2] = Field(default_factory=list)


class ProductionRolloutJob(Contract):
    mode: Literal["PRODUCTION_ROLLOUT"]
    job_id: KubernetesName
    approval_id: Text
    plan_id: Text
    revision: Positive
    manifest_digest: Digest
    environment_id: Text
    agent_id: KubernetesName
    cluster_uid: Text
    namespace: KubernetesName
    storage_class: KubernetesName
    database: RolloutDatabase
    application: RolloutApplication

    @model_validator(mode="after")
    def artifact_kinds(self):
        for part in (self.database, self.application):
            if part.chart.kind != "CHART" or part.values.kind != "VALUES":
                raise ValueError("Chart and Values artifacts required")
        return self

    def artifact_refs(self) -> list[ArtifactRef]:
        return [
            self.database.chart,
            self.database.values,
            self.application.chart,
            self.application.values,
        ]


def rollout_job_id(plan_id: str, revision: int) -> str:
    """One Agent job per immutable Plan Revision; retries reuse the same ID."""
    return "rollout-" + hashlib.sha256(f"{plan_id}:{revision}".encode()).hexdigest()[:24]


RolloutState = Literal["NOT_DISPATCHED", "WAITING_AGENT", "RUNNING", "READY", "FAILED", "UNKNOWN"]
