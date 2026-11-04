"""ProvisioningResult v1: the handoff from Provisioning to Data/Traffic Migration (ticket 13).

Holds identifiers, endpoints and Secret *locations* only. Passwords, `.env` values, registry
tokens, kubeconfig and Terraform State never belong here (PROV-021). `data_state` is always
`NOT_MIGRATED`: a ready target is not a completed migration (PROV-020).
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from cmp_contracts.plan import Scope
from cmp_contracts.provisioning import Contract, Positive, Text

ResultStatus = Literal["READY", "INFRASTRUCTURE_READY", "FAILED", "UNKNOWN"]
CheckStatus = Literal["PASSED", "FAILED", "NOT_RUN", "UNKNOWN"]
CheckLayer = Literal["INFRASTRUCTURE", "WORKLOAD", "APPLICATION"]
FailureStage = Literal[
    "PRECONDITION", "INFRASTRUCTURE", "WORKLOAD_DEPLOY", "POST_DEPLOY_VERIFICATION"
]
Recoverability = Literal["RETRY_AFTER_FIX", "CHECK_STATE_FIRST", "NOT_RECOVERABLE"]
Consumer = Literal["DATA_MIGRATION", "TRAFFIC_MIGRATION"]


class ClusterRef(Contract):
    kind: Literal["EKS", "GKE", "K3s"]
    name: Text | None = None
    location: Text | None = None
    account_id: Text | None = None  # AWS account / GCP project; not exposed by the public API
    agent_id: Text | None = None  # ON_PREM: where Network Preflight runs
    cluster_uid: Text | None = None
    # Known non-secret network identifiers only; routed CIDRs are not known to Provisioning.
    network_refs: dict[str, str] = Field(default_factory=dict)


class SecretLocation(Contract):
    namespace: Text
    name: Text
    key: Text


class DatabaseStorage(Contract):
    pvc: Text | None = None
    storage_class: Text | None = None
    size_gib: Positive | None = None


class DatabaseHandoff(Contract):
    engine: Literal["postgresql"] = "postgresql"
    version: Text | None = None
    host: Text | None = None
    port: Positive | None = None
    name: Text | None = None
    user: Text | None = None
    password_secret: SecretLocation | None = None
    replication_capable: bool | None = None  # measured right after deploy; None = not verified
    exposure: Literal["CLUSTER_INTERNAL"] = "CLUSTER_INTERNAL"
    storage: DatabaseStorage = Field(default_factory=DatabaseStorage)
    release: Text | None = None
    ready: bool = False


class IngressRef(Contract):
    kind: Literal["ALB", "GKE_INGRESS", "EXISTING_K3S"]
    name: Text | None = None
    address: Text | None = None


class ApplicationHandoff(Contract):
    release: Text | None = None
    image: Text | None = None
    internal_endpoint: Text | None = None
    external_endpoint: Text | None = None  # traffic destination; None until an LB is reachable
    ingress: IngressRef | None = None
    port: Positive | None = None
    health_path: Text | None = None
    scheme: Literal["http"] = "http"  # P0: HTTP only, no domain/TLS
    tls: bool = False
    replicas: Positive | None = None
    ready: bool = False


class Check(Contract):
    layer: CheckLayer
    name: Text
    status: CheckStatus
    code: Text | None = None
    reason: Text | None = None


class Failure(Contract):
    stage: FailureStage
    code: Text
    recoverability: Recoverability


class ProvisioningResult(Contract):
    contract_version: Literal["1"] = "1"
    migration_id: Text
    plan_id: Text
    revision: Positive
    approval_id: Text
    provider: Literal["AWS", "GCP", "ON_PREM"]
    scope: Scope
    status: ResultStatus
    code: Text
    data_state: Literal["NOT_MIGRATED"] = "NOT_MIGRATED"
    cluster: ClusterRef
    namespace: Text | None = None
    finished_at: datetime
    database: DatabaseHandoff | None = None
    application: ApplicationHandoff | None = None
    verification: list[Check]
    failure: Failure | None = None
    warnings: list[Text] = Field(default_factory=list)
