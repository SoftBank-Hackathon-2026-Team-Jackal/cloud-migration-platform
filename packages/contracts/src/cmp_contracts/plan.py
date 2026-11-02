"""Minimal immutable-by-storage Plan/Preview data contracts, not an approval API."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_contracts.artifact import ArtifactRef, Digest
from cmp_contracts.chart import KubernetesName
from cmp_contracts.provisioning import (
    Contract,
    PlanRef,
    Positive,
    Text,
    ValidationIssue,
    VersionRef,
)

Scope = Literal["INFRASTRUCTURE", "WORKLOADS", "FULL"]
ArtifactRole = Literal[
    "spec",
    "terraform_plan",
    "database_chart",
    "database_values",
    "application_chart",
    "application_values",
    "bindings",
]
ImageURI = Annotated[str, Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")]


class CloudIdentity(Contract):
    provider: Literal["AWS", "GCP"]
    environment_id: Text
    account_id: Text  # AWS account or GCP project; verified by the calling Activity.
    region: Text
    state_ref: VersionRef  # Stable state identity; not a new state per Plan Revision.


class OnPremIdentity(Contract):
    provider: Literal["ON_PREM"]
    environment_id: Text
    agent_id: Text
    cluster_ref: VersionRef
    namespace: KubernetesName


TargetIdentity = Annotated[CloudIdentity | OnPremIdentity, Field(discriminator="provider")]


class PlanManifest(Contract):
    contract_version: Literal["1"]
    digest_format: Literal["cmp-json-v1"]
    migration_id: Text
    plan: PlanRef
    target: TargetIdentity
    scope: Scope
    image_uri: ImageURI | None = None
    artifacts: dict[ArtifactRole, ArtifactRef]

    @model_validator(mode="after")
    def check_artifact_roles(self):
        expected = {
            "spec": "SPEC",
            "terraform_plan": "TERRAFORM_PLAN",
            "database_chart": "CHART",
            "database_values": "VALUES",
            "application_chart": "CHART",
            "application_values": "VALUES",
            "bindings": "BINDINGS",
        }
        for role, artifact in self.artifacts.items():
            if artifact.kind != expected[role]:
                raise ValueError("Artifact kind does not match its role")
        if self.target.provider == "ON_PREM" and "terraform_plan" in self.artifacts:
            raise ValueError("ON_PREM does not execute a Terraform Plan")
        if self.target.provider == "ON_PREM" and self.scope == "INFRASTRUCTURE":
            raise ValueError("P0 ON_PREM uses an existing K3s cluster")
        return self


class ResourceChange(Contract):
    resource_type: Literal["NETWORK", "CLUSTER", "NODE", "APP", "DATABASE", "STORAGE", "INGRESS"]
    action: Literal["CREATE", "UPDATE", "DELETE", "READ", "NO_CHANGE"]
    name: Text
    quantity: Positive | None = None
    # Only reviewed, non-secret display values belong here; raw Terraform JSON is excluded.
    size: Text | None = None


class Preview(Contract):
    contract_version: Literal["1"]
    manifest_digest: Digest
    changes: list[ResourceChange]
    applied_defaults: list[ResourceChange] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    errors: list[ValidationIssue] = Field(default_factory=list)
    pending_decisions: list[ValidationIssue] = Field(default_factory=list)


class ApprovalTarget(Contract):
    """Identity to store with approval, NOT a user's approval or authorization proof."""

    plan_id: Text
    revision: Positive
    scope: Scope
    manifest_digest: Digest
    preview_digest: Digest
