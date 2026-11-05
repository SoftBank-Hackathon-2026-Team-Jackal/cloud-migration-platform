"""Resolved v2 Planning inputs; no cloud access, secret values or Plan identity."""

from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from cmp_contracts.application import EnvName
from cmp_contracts.provisioning import (
    ContentRef,
    Contract,
    Platform,
    Positive,
    Text,
    ValidationIssue,
    VersionRef,
)

ResourceId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
ImageUri = Annotated[str, Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")]


class S3ArchiveRef(Contract):
    """Content-addressed source or test archive, optionally pinned to an S3 version."""

    uri: Text
    version_id: Text | None = None
    sha256: Sha256

    @model_validator(mode="after")
    def hash_named_archive(self):
        parts = urlsplit(self.uri)
        if (
            parts.scheme != "s3"
            or not parts.netloc
            or not parts.path.strip("/")
            or parts.query
            or parts.fragment
            or ".." in PurePosixPath(parts.path).parts
            or PurePosixPath(parts.path).name != f"{self.sha256}.tar.gz"
        ):
            raise ValueError("Expected a hash-named S3 archive")
        return self


class DatabaseIdentity(Contract):
    name: Text
    username: Text


class ResourceOutputBinding(Contract):
    resource_id: ResourceId
    # connection_url_secret_ref: platform-made URL Secret for single-variable apps (D-16, #79).
    output: Literal[
        "host", "port", "name", "username", "password_secret_ref", "connection_url_secret_ref"
    ]


class ApplicationRequirement(Contract):
    id: ResourceId
    kind: Literal["APPLICATION"]
    replicas: Positive
    cpu_millicores: Positive
    memory_mib: Positive
    port: int = Field(ge=1, le=65535)
    health_path: Annotated[str, Field(pattern=r"^/\S*$")]
    env_bindings: dict[EnvName, ResourceOutputBinding] = Field(default_factory=dict)


class PostgresRequirement(Contract):
    id: ResourceId
    kind: Literal["POSTGRESQL"]
    version: Literal["16"]
    extensions: list[Text] = Field(default_factory=list)
    database: DatabaseIdentity
    cpu_millicores: Positive
    memory_mib: Positive
    storage_gib: Positive


ResourceRequirement = Annotated[
    ApplicationRequirement | PostgresRequirement, Field(discriminator="kind")
]


class NetworkIntent(Contract):
    source_resource_id: ResourceId
    destination_resource_id: ResourceId | None = None
    external_host: Text | None = None
    port: int = Field(ge=1, le=65535)
    protocol: Literal["TCP", "UDP"] = "TCP"

    @model_validator(mode="after")
    def one_destination(self):
        if (self.destination_resource_id is None) == (self.external_host is None):
            raise ValueError("Exactly one network destination is required")
        return self


class RequirementsV2(Contract):
    contract_version: Literal["2"]
    resources: list[ResourceRequirement] = Field(min_length=1)
    network_intent: list[NetworkIntent] = Field(default_factory=list)


class AnalysisInputV2(Contract):
    result_ref: VersionRef
    requirements_ref: ContentRef
    source_refs: dict[ResourceId, S3ArchiveRef]
    config_artifact_ref: ContentRef | None = None


class BuildEvidenceV2(Contract):
    resource_id: ResourceId
    result_ref: VersionRef
    status: Literal["SUCCEEDED", "FAILED"]
    source_ref: S3ArchiveRef
    image_uri: ImageUri | None = None
    image_digest: ImageDigest | None = None
    platforms: list[Platform] = Field(default_factory=list)


class ValidatedWorkloadV2(Contract):
    resource_id: ResourceId
    build_ref: VersionRef
    image_digest: ImageDigest
    platform: Platform
    source_sha256: Sha256


class SandboxEvidenceV2(Contract):
    result_ref: VersionRef
    status: Literal["PASSED", "FAILED"]
    validated_workloads: list[ValidatedWorkloadV2]
    requirements_ref: ContentRef
    report_ref: ContentRef
    test_suite_ref: S3ArchiveRef
    cleanup_status: Literal["CLEANED", "FAILED", "PENDING"]
    config_artifact_ref: ContentRef | None = None


class TargetSelectionV2(Contract):
    ref: VersionRef
    provider: Literal["AWS", "GCP", "ON_PREM"]
    location: Text | None
    access_ref: VersionRef
    capabilities_ref: VersionRef
    profile: Text | None = None  # Optional defaults, never an override of requirements.

    @model_validator(mode="after")
    def provider_location(self):
        if (self.provider == "ON_PREM") != (self.location is None):
            raise ValueError("Cloud targets require a location; ON_PREM has none")
        return self


class EnvironmentInputV2(Contract):
    env_file_ref: ContentRef
    mapping_ref: ContentRef


class PlanningPolicyV2(Contract):
    template_version: Text


class PlanningRequestV2(Contract):
    contract_version: Literal["2"]
    migration_id: Text
    analysis: AnalysisInputV2
    builds: list[BuildEvidenceV2]
    sandbox: SandboxEvidenceV2
    target_selection: TargetSelectionV2
    environment: EnvironmentInputV2
    policy: PlanningPolicyV2


class ValidatedImageV2(Contract):
    resource_id: ResourceId
    build_ref: VersionRef
    source_ref: S3ArchiveRef
    image_uri: ImageUri
    image_digest: ImageDigest
    platform: Platform


class ProvisioningSpecV2(Contract):
    """Validated Planning input, before Plan ID/Revision or provider rendering exists."""

    contract_version: Literal["2"]
    migration_id: Text
    analysis_result_ref: VersionRef
    requirements_ref: ContentRef
    requirements: RequirementsV2
    source_refs: dict[ResourceId, S3ArchiveRef]
    config_artifact_ref: ContentRef | None = None
    sandbox_result_ref: VersionRef
    sandbox_report_ref: ContentRef
    sandbox_cleanup_status: Literal["CLEANED", "FAILED", "PENDING"]
    test_suite_ref: S3ArchiveRef
    images: list[ValidatedImageV2]
    target_selection: TargetSelectionV2
    environment: EnvironmentInputV2
    policy: PlanningPolicyV2


class PlanningResultV2(Contract):
    spec: ProvisioningSpecV2
    pending_decisions: list[ValidationIssue] = Field(default_factory=list)
