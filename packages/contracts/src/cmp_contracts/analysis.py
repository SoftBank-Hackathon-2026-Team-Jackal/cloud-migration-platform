"""Contracts for analysis and evidence of migration requirements."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from cmp_contracts.provisioning import ContentRef, Contract, Text, VersionRef
from cmp_contracts.provisioning_v2 import ResourceId, TargetSelectionV2


class ScopeSelection(Contract):
    """What the user picked in the web scope selection (ticket 56): where the app runs."""

    connection_id: Text
    region: Text
    cluster: Text
    namespace: Text
    workload: Text
    workload_kind: Text


class ScopeEvidence(Contract):
    kind: Text
    location: Text


class ScopeCandidate(Contract):
    """One resource the platform found from the selected app (ticket 56), with its evidence."""

    candidate_id: Text
    resource_type: Text
    name: Text
    arn: Text | None = None
    evidence: list[ScopeEvidence] = Field(default_factory=list)
    suggested: bool = False
    selected: bool = False  # 06C: the user's choice; unselected candidates are passed too


class ConfirmedScope(Contract):
    """The user's confirmed analysis scope (ticket 06B, 06C). Built by the API from its stored
    records, never from values the web sends. Names and ARNs only, no credentials. Every
    candidate the platform found is here, told apart by `selected`."""

    scope_id: Text
    selection: ScopeSelection
    candidates: list[ScopeCandidate]


class AnalysisInput(Contract):
    """Input of the analysis child Workflow. Only `migration_id` is required so a scaffold run
    (no source yet) keeps working; everything else describes where the real analysis reads from.
    Credentials never belong here."""

    migration_id: Text
    repository: Text | None = None
    requested_commit: Text | None = None
    manifests_dir: Text | None = None  # relative to the checked-out workspace
    cluster_name: Text | None = None  # source EKS cluster, read-only AWS scan when set
    credential_ref: Text | None = None  # named local profile, never a key
    # Reference to the user's access token for a private repository. A name, never a token:
    # the worker resolves it only when it clones (ticket 23).
    source_access_ref: VersionRef | None = None
    role_arns: list[Text] = Field(default_factory=list)
    # Ticket 06B: the region of the source cluster (the worker setting is the fallback) and the
    # scope the user confirmed, filled by the API from stored records.
    region: Text | None = None
    scope: ConfirmedScope | None = None
    # ENV-02: the uploaded `.env` (POST /env-files). Passed through only; ENV-05 reads key names
    # from it instead of the repository's `.env*` files.
    env_file_ref: ContentRef | None = None


class AnalysisRequest(Contract):
    migration_id: Text
    repository: Text
    application_id: Text
    env_file_ref: ContentRef
    source_access_ref: VersionRef
    target_selection: TargetSelectionV2


class UsageProfile(Contract):
    usages: list[str] = Field(default_factory=list)


class PrecheckReport(Contract):
    findings: list[str] = Field(default_factory=list)


class Evidence(Contract):
    kind: Literal["CODE", "MANIFEST", "CLOUD_RESOURCE", "IAM_POLICY", "CLOUDTRAIL", "CONFIG"]
    locator: str
    verified: bool = False


class IamRoleUsage(Contract):
    """An app role the scan read (06C): what it is allowed to do. Wildcard resources cannot be
    narrowed to a resource, so they are flagged instead of guessed."""

    role_arn: Text
    actions: list[Text] = Field(default_factory=list)
    wildcard_resources: bool = False
    evidence: Evidence


class CloudTrailCall(Contract):
    """Management events the scan saw (06C). CloudTrail `LookupEvents` covers the whole account
    and no data events: it never proves that an app did not use something."""

    event_source: Text
    event_name: Text
    count: int = Field(ge=0)
    evidence: Evidence


class AwsUsage(Contract):
    """What the AWS scan says about resource use (06C). `status` says whether it ran, so a
    missing scan is not mistaken for "nothing found"."""

    status: Literal["COLLECTED", "NOT_COLLECTED", "FAILED"]
    reason: Text | None = None
    iam_roles: list[IamRoleUsage] = Field(default_factory=list)
    cloudtrail: list[CloudTrailCall] = Field(default_factory=list)


SecurityCriterion = Literal[
    "EXTERNAL_HTTPS", "DB_ACCESS_SCOPE", "DB_TRANSPORT_ENCRYPTION", "ADMIN_API_EXPOSURE"
]
# From strongest to weakest: PROTECTED, PARTIAL (some entries or paths weak), UNPROTECTED. UNKNOWN is
# "no evidence" and is never read as protected. NOT_APPLICABLE: the thing measured is absent.
SecurityState = Literal["PROTECTED", "PARTIAL", "UNPROTECTED", "UNKNOWN", "NOT_APPLICABLE"]


class SecurityMeasure(Contract):
    """The source's state for one security criterion (ticket 28-B), with its evidence.

    `reasons` are codes, never values: no connection string, secret or address."""

    criterion: SecurityCriterion
    state: SecurityState
    reasons: list[Text] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class MappingRow(Contract):
    id: Text
    capability: str
    usage: str
    grade: Literal["DIRECT", "API_DIFF", "PARTIAL", "NONE"]
    required_proposed: bool
    required_reason: str
    required: bool | None
    disposition: Literal["MIGRATE", "KEEP"]
    loses: list[str]
    treatment: Literal["PROCEED", "APPROVAL", "BLOCK"] | None
    status: Literal["UNVERIFIED", "EVIDENCE_CHECKED", "SANDBOX_VERIFIED"]
    evidence: list[Evidence]


class ReviewRecord(Contract):
    analysis_ref: VersionRef
    required: dict[Text, bool]
    operational: list[str]
    approvals: dict[Text, Literal["APPROVED", "REJECTED"]]
    actor: str
    decided_at: datetime


class ConfirmRequirementsSignal(Contract):
    """User confirms which mapping rows are required; keys are mapping row IDs."""

    analysis_ref: VersionRef
    required: dict[Text, bool]
    operational: list[Text] = Field(default_factory=list)
    actor: Text


class ApproveLossesSignal(Contract):
    """User approves or rejects the loss of each APPROVAL row; keys are mapping row IDs."""

    analysis_ref: VersionRef
    approvals: dict[Text, Literal["APPROVED", "REJECTED"]]
    actor: Text


class EnvExceptions(Contract):
    excluded: list[str]
    replaced: dict[str, str]
    missing: list[str]


class ExternalEntry(Contract):
    protocol: Literal["HTTP", "HTTPS"]
    port: int = Field(ge=1, le=65535)
    target_resource_id: ResourceId
    target_port: int = Field(ge=1, le=65535)
    source_service: str


class AnalysisReport(Contract):
    migration_id: str
    ref: VersionRef
    base_commit: str
    commit_verified: bool
    usage_profile: UsageProfile
    mapping: list[MappingRow]
    precheck: PrecheckReport
    external_entries: list[ExternalEntry]
    source_cidrs: list[str]
    source_db_images: dict[ResourceId, str]
    unmapped_items: list[str]
    unverified_risks: list[str]
    scope: ConfirmedScope | None = None  # ticket 06B: what the user confirmed to analyze
    aws_usage: AwsUsage | None = None  # ticket 06C: IAM and CloudTrail evidence of resource use
    security: list[SecurityMeasure] | None = None  # ticket 28-B: None before it was measured

    @model_validator(mode="after")
    def unique_mapping_ids(self):
        ids = [row.id for row in self.mapping]
        if len(ids) != len(set(ids)):
            raise ValueError("Mapping row IDs must be unique within an analysis version")
        return self
