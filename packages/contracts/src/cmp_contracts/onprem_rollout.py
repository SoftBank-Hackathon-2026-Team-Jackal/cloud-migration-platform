"""Approved ON_PREM rollout Agent work (ticket 12, E2).

Only identifiers, ArtifactRefs and Secret names/keys cross this boundary. Values/Chart bytes are
downloaded separately and verified by digest; Secret values never appear here.
"""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_contracts.artifact import ArtifactRef, Digest
from cmp_contracts.chart import KubernetesName, SecretKey
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


class RolloutEnvSecret(Contract):
    """ENV-04: a Secret the platform renders from the approved EnvPlan (`<app>-env`,
    `<db>-conn`). Only the name and keys cross this boundary; the Agent fetches the data for its
    own RUNNING job from `GET /agents/{id}/jobs/{job}/env-secrets` and applies it in the cluster."""

    name: KubernetesName
    keys: list[SecretKey] = Field(min_length=1)


class RolloutApplication(Contract):
    release: ReleaseName
    chart: ArtifactRef
    values: ArtifactRef
    port: int = Field(ge=1, le=65535)
    health_path: Annotated[str, Field(pattern=r"^/[A-Za-z0-9/_.-]*$")]
    # User-provided Secrets that must already exist; checked before any Helm mutation.
    secret_refs: list[SecretRefV2] = Field(default_factory=list)
    # ENV-04: platform-rendered Secrets. Omitted while empty so jobs without an EnvPlan keep
    # identical payloads; an Agent older than ENV-04 rejects a job that carries it.
    env_secrets: list[RolloutEnvSecret] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )


class RolloutImagePull(Contract):
    """Ticket 29 (I4): the approved images are in the platform ECR. The Agent fetches a
    short-lived credential for its job from `GET /agents/{id}/jobs/{job}/image-pull` and keeps
    the docker-registry Secret `secret` refreshed; no token crosses this boundary."""

    secret: KubernetesName
    registry: Annotated[str, Field(pattern=r"^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com$")]


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
    # Ticket 29: omitted when no image needs ECR auth so earlier jobs keep identical payloads;
    # an Agent older than ticket 29 rejects a job that carries it (UNSUPPORTED_JOB).
    image_pull: RolloutImagePull | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

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


class RolloutReplication(Contract):
    """Ticket 27: the Agent's replication-capable check, reported with a READY rollout."""

    capable: bool
    code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,64}$")] | None = None


def rollout_job_id(plan_id: str, revision: int) -> str:
    """One Agent job per immutable Plan Revision; retries reuse the same ID."""
    return "rollout-" + hashlib.sha256(f"{plan_id}:{revision}".encode()).hexdigest()[:24]


RolloutState = Literal["NOT_DISPATCHED", "WAITING_AGENT", "RUNNING", "READY", "FAILED", "UNKNOWN"]
