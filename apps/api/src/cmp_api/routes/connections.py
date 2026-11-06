"""Environment connection API for the web BFF (ticket 23, ADR-005).

Every route needs the operator key (`X-Enrollment-Admin-Key`, as for Agent administration) and is
called server-side by the web BFF only; no CORS is configured. Responses never contain keys or
STS credentials; verification codes are stable and never echo AWS/Google messages.
"""

import re
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cmp_api import connections as svc
from cmp_api import scope_selection as scope
from cmp_api.config import settings
from cmp_api.connections import (
    AWS_KINDS,
    AWS_REGION,
    GCP_REGION,
    ROLE_ARN,
    AwsConnector,
    ConnectionFailure,
    EnvironmentConnection,
)
from cmp_api.gcp_connection_check import TemporalGcpChecker
from cmp_api.routes.agents import SessionDep, require_admin
from cmp_api.scope_selection import CLUSTER_NAME, NAMESPACE, WORKLOAD_NAME, KubeFactory


class InputHidingRoute(APIRoute):
    """422 bodies keep location and type only, so a pasted key is never echoed back."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                detail = [
                    {key: error[key] for key in ("type", "loc", "msg") if key in error}
                    for error in exc.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": detail})

        return route


router = APIRouter(
    tags=["environment connections"],
    dependencies=[Depends(require_admin)],
    route_class=InputHidingRoute,
)

Kind = Literal["AWS_SOURCE", "AWS_TARGET", "GCP_TARGET"]
Status = Literal["PENDING", "CONNECTED", "FAILED"]
_STATUS = {
    "CONNECTION_NOT_FOUND": 404,
    "INVALID_ROLE_ARN": 422,
    # Ticket 56: scope selection
    "CLUSTER_NOT_FOUND": 404,
    "NAMESPACE_NOT_FOUND": 404,
    "WORKLOAD_NOT_FOUND": 404,
    "ANALYSIS_NOT_FOUND": 404,
    "INVALID_SCOPE": 400,
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class GcpTarget(Strict):
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    expected_principal: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9][a-z0-9.-]{1,190}\.[a-z]{2,}$"
    )


class CreateConnectionRequest(Strict):
    kind: Kind
    display_name: str = Field(min_length=1, max_length=120)
    region: str = Field(max_length=40)
    gcp: GcpTarget | None = None

    @model_validator(mode="after")
    def _kind_fields(self):
        if self.kind in AWS_KINDS:
            if self.gcp is not None:
                raise ValueError("gcp fields are only for GCP_TARGET")
            if not re.fullmatch(AWS_REGION, self.region):
                raise ValueError("invalid AWS region")
        else:
            if self.gcp is None:
                raise ValueError("GCP_TARGET needs gcp.project_id and gcp.expected_principal")
            if not re.fullmatch(GCP_REGION, self.region):
                raise ValueError("invalid GCP region")
            allowed = settings.connection_gcp_project_id
            if allowed and self.gcp.project_id != allowed:
                raise ValueError("GCP_PROJECT_NOT_ALLOWED: the demo uses the platform project")
        return self


class CreateConnectionResponse(BaseModel):
    connection_id: str
    kind: Kind
    display_name: str
    status: Status
    external_id: str | None = None
    launch_stack_url: str | None = None
    # Ticket 46: the `PlatformPrincipalArn` stack parameter (AWS only; null when unconfigured).
    platform_principal_arn: str | None = None


class RoleRequest(Strict):
    role_arn: str = Field(pattern=ROLE_ARN, max_length=600)


class ConnectionDetail(BaseModel):
    connection_id: str
    kind: Kind
    display_name: str
    region: str
    status: Status
    account_id: str | None = None
    project_id: str | None = None
    role_arn: str | None = None
    external_id: str | None = None
    expected_principal: str | None = None
    principal: str | None = None
    credential_ref: str | None = None
    launch_stack_url: str | None = None
    platform_principal_arn: str | None = None  # ticket 46
    stack_name: str | None = None
    last_code: str | None = None
    last_checked_at: int | None = None
    last_verified_at: int | None = None


class VerifyResponse(BaseModel):
    """`account_id` for AWS, `project_id` for GCP; `eks_clusters` only for a verified source."""

    status: Literal["CONNECTED", "FAILED"]
    code: str
    checked_at: int
    account_id: str | None = None
    project_id: str | None = None
    principal: str | None = None
    eks_clusters: list[str] | None = None


class AwsConnectionSummary(BaseModel):
    connection_id: str
    kind: Literal["AWS_SOURCE", "AWS_TARGET"]
    display_name: str
    account_id: str | None
    region: str
    status: Status
    last_verified_at: int | None


class GcpConnectionSummary(BaseModel):
    connection_id: str
    kind: Literal["GCP_TARGET"]
    display_name: str
    project_id: str | None
    region: str
    status: Status
    last_verified_at: int | None


class TunnelSummary(BaseModel):
    tunnel_ip: str
    last_handshake_at: int | None
    app_health_ok: bool
    db_connect_ok: bool
    # Ticket 25 F6: true when this report is no longer current evidence (report older than
    # TUNNEL_STATUS_TTL_SECONDS or no handshake within TUNNEL_HANDSHAKE_STALE_SECONDS).
    stale: bool
    fresh_until: int | None


class AgentSummary(BaseModel):
    agent_id: str
    cluster_uid: str
    last_seen_at: int | None
    online: bool
    revoked: bool
    version: str | None
    tunnel: TunnelSummary | None


class ConnectionList(BaseModel):
    connections: list[AwsConnectionSummary | GcpConnectionSummary]
    agents: list[AgentSummary]


class AgentList(BaseModel):
    agents: list[AgentSummary]


class DeleteResponse(BaseModel):
    connection_id: str
    deleted: Literal[True]
    stack_name: str | None
    notice: str


class ClusterList(BaseModel):
    connection_id: str
    region: str
    eks_clusters: list[str]


class NamespaceList(BaseModel):
    connection_id: str
    region: str
    cluster: str
    namespaces: list[str]


class Workload(BaseModel):
    kind: Literal["Deployment", "StatefulSet"]
    name: str


class WorkloadList(BaseModel):
    connection_id: str
    region: str
    cluster: str
    namespace: str
    workloads: list[Workload]


class ScopeAnalysisRequest(Strict):
    region: str = Field(pattern=AWS_REGION)
    cluster: str = Field(pattern=CLUSTER_NAME)
    namespace: str = Field(pattern=NAMESPACE)
    workload: str = Field(pattern=WORKLOAD_NAME)
    # Not in the web's ScopeSelection: tells a Deployment from a StatefulSet of the same name.
    # Omitted, a Deployment is tried first.
    workload_kind: Literal["Deployment", "StatefulSet"] | None = None


class ScopeSelection(BaseModel):
    connection_id: str
    region: str
    cluster: str
    namespace: str
    workload: str
    workload_kind: Literal["Deployment", "StatefulSet"]


class ScopeEvidence(BaseModel):
    kind: Literal["CODE", "MANIFEST", "CLOUD_RESOURCE", "IAM_POLICY", "CLOUDTRAIL", "CONFIG"]
    location: str


class ScopeCandidate(BaseModel):
    candidate_id: str
    resource_type: str
    name: str
    arn: str | None
    evidence: list[ScopeEvidence]
    suggested: bool


class ScopeAnalysis(BaseModel):
    analysis_id: str
    selection: ScopeSelection
    candidates: list[ScopeCandidate]


class ScopeConfirmRequest(Strict):
    included: list[str] = Field(max_length=500)


class ScopeConfirmation(BaseModel):
    scope_id: str
    analysis_id: str
    included: list[str]
    excluded: list[str]
    confirmed_at: str  # ISO-8601 UTC, as the web type (not epoch seconds like other routes)


def get_aws_connector() -> AwsConnector:
    return AwsConnector(settings.connection_platform_aws_profile)


def get_gcp_checker() -> Callable[[str], str]:
    # Ticket 48 G7: the GCP identity lives in the provisioning worker; the API asks it.
    return TemporalGcpChecker()


def get_kube_factory() -> KubeFactory:
    return scope.default_kube_factory


AwsDep = Annotated[AwsConnector, Depends(get_aws_connector)]
KubeDep = Annotated[KubeFactory, Depends(get_kube_factory)]
ClusterPath = Annotated[str, Path(pattern=CLUSTER_NAME)]
NamespacePath = Annotated[str, Path(pattern=NAMESPACE)]
RegionQuery = Annotated[str | None, Query(pattern=AWS_REGION)]
GcpDep = Annotated[Callable[[str], str], Depends(get_gcp_checker)]


def _fail(exc: ConnectionFailure):
    raise HTTPException(status_code=_STATUS.get(exc.code, 409), detail=exc.code) from None


def _sync(session) -> None:
    svc.sync_aws_config(
        session,
        path=settings.connection_aws_config_file,
        base_path=settings.connection_aws_base_config_file,
        platform_profile=settings.connection_platform_aws_profile,
    )


def _launch_url(connection: EnvironmentConnection) -> str | None:
    return svc.launch_stack_url(
        connection,
        template_base_url=settings.connection_template_base_url,
        principal_arn=settings.connection_platform_principal_arn,
    )


def _detail(connection: EnvironmentConnection) -> dict:
    aws = connection.kind in AWS_KINDS
    return {
        "connection_id": connection.id,
        "kind": connection.kind,
        "display_name": connection.display_name,
        "region": connection.region,
        "status": connection.status,
        "account_id": connection.account_id,
        "project_id": connection.project_id,
        "role_arn": connection.role_arn,
        "external_id": connection.external_id,
        "expected_principal": connection.expected_principal,
        "principal": connection.principal,
        "credential_ref": svc.credential_ref(connection),
        "launch_stack_url": _launch_url(connection) if aws else None,
        "platform_principal_arn": settings.connection_platform_principal_arn if aws else None,
        "stack_name": svc.stack_name(connection) if aws else None,
        "last_code": connection.last_code,
        "last_checked_at": connection.last_checked_at,
        "last_verified_at": connection.last_verified_at,
    }


def _summary(connection: EnvironmentConnection) -> dict:
    identity = (
        {"account_id": connection.account_id}
        if connection.kind in AWS_KINDS
        else {"project_id": connection.project_id}
    )
    return {
        "connection_id": connection.id,
        "kind": connection.kind,
        "display_name": connection.display_name,
        **identity,
        "region": connection.region,
        "status": connection.status,
        "last_verified_at": connection.last_verified_at,
    }


@router.post("/connections", response_model=CreateConnectionResponse, status_code=201)
def create(request: CreateConnectionRequest, session: SessionDep):
    connection = svc.create_connection(
        session,
        kind=request.kind,
        display_name=request.display_name,
        region=request.region,
        project_id=request.gcp.project_id if request.gcp else None,
        expected_principal=request.gcp.expected_principal if request.gcp else None,
    )
    return {
        "connection_id": connection.id,
        "kind": connection.kind,
        "display_name": connection.display_name,
        "status": connection.status,
        "external_id": connection.external_id,
        "launch_stack_url": _launch_url(connection),
        "platform_principal_arn": settings.connection_platform_principal_arn
        if connection.kind in AWS_KINDS
        else None,
    }


@router.get("/connections", response_model=ConnectionList)
def unified_list(session: SessionDep):
    """Cloud connections plus on-prem Agents and their last tunnel report, in one call."""
    return {
        "connections": [_summary(c) for c in svc.list_connections(session)],
        "agents": svc.agent_summaries(session),
    }


@router.get("/connections/{connection_id}", response_model=ConnectionDetail)
def detail(connection_id: str, session: SessionDep):
    try:
        return _detail(svc.get_connection(session, connection_id))
    except ConnectionFailure as exc:
        _fail(exc)


@router.put("/connections/{connection_id}/role", response_model=ConnectionDetail)
def register_role(connection_id: str, request: RoleRequest, session: SessionDep):
    try:
        connection = svc.set_role(session, connection_id, request.role_arn)
    except ConnectionFailure as exc:
        _fail(exc)
    _sync(session)
    return _detail(connection)


@router.post(
    "/connections/{connection_id}/verify",
    response_model=VerifyResponse,
    response_model_exclude_unset=True,
)
def verify(connection_id: str, session: SessionDep, aws: AwsDep, gcp: GcpDep):
    """Always 200 with CONNECTED/FAILED and a stable code once the connection exists."""
    try:
        return svc.verify_connection(session, connection_id, aws=aws, gcp=gcp)
    except ConnectionFailure as exc:
        _fail(exc)


@router.get("/connections/{connection_id}/eks-clusters", response_model=ClusterList)
def eks_clusters(
    connection_id: str,
    session: SessionDep,
    aws: AwsDep,
    region: Annotated[str | None, Query(pattern=AWS_REGION)] = None,
):
    """Anchor discovery (regions -> EKS clusters). Following references is Analysis' job."""
    try:
        chosen, names = svc.discover_eks_clusters(session, aws, connection_id, region)
    except ConnectionFailure as exc:
        _fail(exc)
    return {"connection_id": connection_id, "region": chosen, "eks_clusters": names}


@router.get(
    "/connections/{connection_id}/eks-clusters/{cluster}/namespaces", response_model=NamespaceList
)
def eks_namespaces(
    connection_id: str,
    cluster: ClusterPath,
    session: SessionDep,
    aws: AwsDep,
    kube: KubeDep,
    region: RegionQuery = None,
):
    """Ticket 56: namespaces of a source cluster, system namespaces excluded."""
    try:
        opened = scope.open_cluster(session, aws, kube, connection_id, region, cluster)
        names = scope.list_namespaces(opened)
    except ConnectionFailure as exc:
        _fail(exc)
    return {
        "connection_id": connection_id,
        "region": opened.region,
        "cluster": cluster,
        "namespaces": names,
    }


@router.get(
    "/connections/{connection_id}/eks-clusters/{cluster}/namespaces/{namespace}/workloads",
    response_model=WorkloadList,
)
def eks_workloads(
    connection_id: str,
    cluster: ClusterPath,
    namespace: NamespacePath,
    session: SessionDep,
    aws: AwsDep,
    kube: KubeDep,
    region: RegionQuery = None,
):
    """Ticket 56: Deployments and StatefulSets in one namespace."""
    try:
        opened = scope.open_cluster(session, aws, kube, connection_id, region, cluster)
        workloads = scope.list_workloads(opened, namespace)
    except ConnectionFailure as exc:
        _fail(exc)
    return {
        "connection_id": connection_id,
        "region": opened.region,
        "cluster": cluster,
        "namespace": namespace,
        "workloads": workloads,
    }


@router.post("/connections/{connection_id}/scope-analyses", response_model=ScopeAnalysis)
def scope_analysis(
    connection_id: str,
    request: ScopeAnalysisRequest,
    session: SessionDep,
    aws: AwsDep,
    kube: KubeDep,
):
    """Ticket 56: scope candidates from what the platform actually read (stored)."""
    try:
        opened = scope.open_cluster(
            session, aws, kube, connection_id, request.region, request.cluster
        )
        return scope.analyze_scope(
            session,
            opened,
            namespace=request.namespace,
            workload=request.workload,
            workload_kind=request.workload_kind,
        )
    except ConnectionFailure as exc:
        _fail(exc)


@router.post("/scope-analyses/{analysis_id}/confirm", response_model=ScopeConfirmation)
def confirm_scope(analysis_id: str, request: ScopeConfirmRequest, session: SessionDep):
    """Ticket 56: stores the included candidates; the rest of the analysis is excluded."""
    try:
        return scope.confirm_scope(session, analysis_id, request.included)
    except ConnectionFailure as exc:
        _fail(exc)


@router.delete("/connections/{connection_id}", response_model=DeleteResponse)
def delete(connection_id: str, session: SessionDep):
    try:
        kind, name = svc.delete_connection(session, connection_id)
    except ConnectionFailure as exc:
        _fail(exc)
    _sync(session)
    aws = kind in AWS_KINDS
    notice = (
        f"Platform data removed. Delete the CloudFormation stack '{name}' in your AWS account "
        "to remove the role and its trust in the platform."
        if aws
        else "Platform data removed. No resource was created in your project by this connection."
    )
    return {"connection_id": connection_id, "deleted": True, "stack_name": name, "notice": notice}


@router.get("/agent-admin/agents", response_model=AgentList)
def list_agents(session: SessionDep):
    return {"agents": svc.agent_summaries(session)}
