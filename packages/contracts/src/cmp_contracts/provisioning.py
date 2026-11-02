"""Internal v1 Planning contracts; upstream wire adapters and HTTP APIs are separate."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(min_length=1, pattern=r"^\S(?:.*\S)?$")]
Positive = Annotated[int, Field(gt=0)]
Platform = Literal["linux/amd64", "linux/arm64"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class VersionRef(Contract):
    """Opaque, immutable record version. Never a credential or signed URL."""

    id: Text
    version: Text


class ContentRef(VersionRef):
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class PlanRef(Contract):
    plan_id: Text
    revision: Positive
    template_version: Text


class Image(Contract):
    uri: Annotated[str, Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")]
    platforms: list[Platform] = Field(min_length=1)
    registry_auth_ref: VersionRef | None = None


class Application(Contract):
    cpu_millicores: Positive
    memory_mib: Positive
    replicas: Positive
    port: int = Field(ge=1, le=65535)
    health_path: Annotated[str, Field(pattern=r"^/\S*$")]


class Database(Contract):
    version: Literal["16"]
    name: Text
    username: Text
    cpu_millicores: Positive
    memory_mib: Positive
    storage_gib: Positive
    auth_policy: Literal["GENERATE"]


class Requirements(Contract):
    application: Application
    database: Database
    bindings: dict[
        Text,
        Literal[
            "database.host",
            "database.port",
            "database.name",
            "database.username",
            "database.secret_ref",
        ],
    ] = Field(default_factory=dict)


class Nodes(Contract):
    count: Positive
    instance_type: Text
    architecture: Platform
    disk_gib: Positive


class Subnet(Contract):
    cidr: Text
    role: Literal["PUBLIC", "PRIVATE"]
    zone: Text | None = None
    region: Text | None = None


class Network(Contract):
    mode: Literal["CREATE", "EXISTING"]
    network_ref: VersionRef | None = None
    vpc_cidr: Text | None = None
    subnets: list[Subnet] = Field(min_length=1)
    existing_cidrs: list[Text] = Field(default_factory=list)


class CloudTarget(Contract):
    profile: Text
    region: Text
    credential_ref: VersionRef
    nodes: Nodes
    network: Network


class AwsTarget(CloudTarget):
    provider: Literal["AWS"]


class GcpTarget(CloudTarget):
    provider: Literal["GCP"]


class OnPremTarget(Contract):
    provider: Literal["ON_PREM"]
    profile: Text
    agent_id: Text
    cluster_ref: VersionRef
    namespace: Text
    storage_class: Text
    architecture: Platform


Target = Annotated[AwsTarget | GcpTarget | OnPremTarget, Field(discriminator="provider")]


class TargetSelection(Contract):
    ref: VersionRef
    target: Target


class StageResult(Contract):
    migration_id: Text
    ref: VersionRef


class AnalysisResult(StageResult):
    source: ContentRef
    config: ContentRef
    requirements_ref: VersionRef
    requirements: Requirements


class SandboxResult(StageResult):
    status: Literal["PASSED", "FAILED"]
    cleanup_status: Literal["CLEANED", "FAILED", "PENDING"]
    analysis_ref: VersionRef
    source: ContentRef
    config: ContentRef
    requirements_ref: VersionRef
    environment_ref: VersionRef
    target_selection_ref: VersionRef
    report_ref: VersionRef


class BuildResult(StageResult):
    status: Literal["SUCCEEDED", "FAILED"]
    sandbox_ref: VersionRef
    source: ContentRef
    config: ContentRef
    image: Image


class PlanningRequest(Contract):
    contract_version: Literal["1"]
    migration_id: Text
    plan: PlanRef
    environment_ref: VersionRef
    target_selection: TargetSelection
    analysis: AnalysisResult
    sandbox: SandboxResult
    build: BuildResult


class InputLineage(Contract):
    analysis_ref: VersionRef
    sandbox_ref: VersionRef
    build_ref: VersionRef
    requirements_ref: VersionRef
    source: ContentRef
    config: ContentRef
    target_selection_ref: VersionRef
    sandbox_report_ref: VersionRef
    sandbox_cleanup_status: Literal["CLEANED", "FAILED", "PENDING"]


class ProvisioningSpec(Contract):
    contract_version: Literal["1"]
    migration_id: Text
    plan: PlanRef
    inputs: InputLineage
    target: Target
    image: Image
    requirements: Requirements
    environment_ref: VersionRef


class ValidationIssue(Contract):
    code: str
    path: str
    message: str


class ProvisioningInputError(ValueError):
    """Safe diagnostics: never include rejected input values or Pydantic context."""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.code}" for i in issues))


class ProviderConfiguration(Contract):
    provider: Literal["AWS", "GCP", "ON_PREM"]
    cluster_kind: Literal["EKS", "GKE", "K3s"]
    network_kind: Literal["AWS_VPC", "GCP_VPC", "EXISTING_K3S"]
    ingress_kind: Literal["ALB", "GKE_INGRESS", "EXISTING_K3S"]
    node_count: int | None = None
    node_type: str | None = None
    node_disk_gib: int | None = None
    database_workload: Literal["StatefulSet"] = "StatefulSet"
    database_pvc_gib: Positive
    application_port: int
    image_uri: str
    # This is a configuration summary, never a successful deployment/Preview claim.
    execution_performed: Literal[False] = False


class PlanningResult(Contract):
    spec: ProvisioningSpec
    configuration: ProviderConfiguration
    pending_decisions: list[ValidationIssue] = Field(default_factory=list)
