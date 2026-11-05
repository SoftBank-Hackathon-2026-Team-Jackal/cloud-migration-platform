"""Source and target environment connections (ticket 23, ADR-005).

AWS connections never take keys: the user creates a role in their own account from the
platform's CloudFormation template, trusting the platform principal with a platform-issued
External ID, and registers only the Role ARN. The platform then only ever holds STS temporary
credentials in memory. GCP uses the platform's fixed project and the existing ADC principal check.

Each AWS connection with a role resolves to a generated named profile `cmp-conn-<hex>` (the
`credential_ref`), so analysis `get_session` and Provisioning `aws_profile` work unchanged.
Nothing secret is stored: Role ARN, External ID and verification metadata only.
"""

import os
import re
import secrets
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from sqlalchemy import Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from cmp_api.agent_registry import RegisteredAgent
from cmp_api.db import Base
from cmp_api.tunnels import TunnelStatus

AWS_KINDS = ("AWS_SOURCE", "AWS_TARGET")
KINDS = (*AWS_KINDS, "GCP_TARGET")
TEMPLATES = {"AWS_SOURCE": "source-role.yaml", "AWS_TARGET": "target-role.yaml"}
PROFILE_PREFIX = "cmp-conn-"
SESSION_SECONDS = 900  # verify/discovery sessions; generated profiles use the role maximum
PROFILE_SESSION_SECONDS = 3600  # Terraform Apply outlives 15 minutes; SDKs refresh on expiry
ONLINE_SECONDS = 60

AWS_REGION = r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d$"
GCP_REGION = r"^[a-z]+-[a-z]+\d$"
ROLE_ARN = r"^arn:aws(-cn|-us-gov)?:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$"
_ROLE_ARN = re.compile(ROLE_ARN)
_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
_CLIENT_CONFIG = Config(retries={"max_attempts": 2}, connect_timeout=5, read_timeout=15)


class ConnectionFailure(Exception):
    """Stable code only; never carries AWS/Google messages or credentials."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EnvironmentConnection(Base):
    __tablename__ = "environment_connections"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    region: Mapped[str] = mapped_column(String(40), nullable=False)
    # AWS: External ID is issued once by the platform; Role ARN is registered once by the user.
    external_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    role_arn: Mapped[str | None] = mapped_column(String(600))
    account_id: Mapped[str | None] = mapped_column(String(12))
    # GCP: the platform's fixed project and the ADC principal expected to act there.
    project_id: Mapped[str | None] = mapped_column(String(30))
    expected_principal: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    principal: Mapped[str | None] = mapped_column(String(600))
    last_code: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[int | None] = mapped_column(Integer)
    last_verified_at: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def credential_ref(connection: EnvironmentConnection) -> str | None:
    if connection.kind not in AWS_KINDS or not connection.role_arn:
        return None
    return PROFILE_PREFIX + connection.id.removeprefix("conn-")


def stack_name(connection: EnvironmentConnection) -> str:
    role = "source" if connection.kind == "AWS_SOURCE" else "target"
    return f"cmp-{role}-{connection.id.removeprefix('conn-')}"


def launch_stack_url(
    connection: EnvironmentConnection, *, template_base_url: str | None, principal_arn: str | None
) -> str | None:
    """CloudFormation quick-create link; needs the S3-hosted template and the platform principal."""
    if connection.kind not in AWS_KINDS or not template_base_url or not principal_arn:
        return None
    query = urlencode(
        {
            "templateURL": f"{template_base_url.rstrip('/')}/{TEMPLATES[connection.kind]}",
            "stackName": stack_name(connection),
            "param_PlatformPrincipalArn": principal_arn,
            "param_ExternalId": connection.external_id,
        },
        quote_via=quote,
    )
    return (
        f"https://{connection.region}.console.aws.amazon.com/cloudformation/home"
        f"?region={connection.region}#/stacks/quickcreate?{query}"
    )


# ------------------------------------------------------------------------------ registration


def create_connection(
    session: Session,
    *,
    kind: str,
    display_name: str,
    region: str,
    project_id: str | None = None,
    expected_principal: str | None = None,
) -> EnvironmentConnection:
    connection = EnvironmentConnection(
        id="conn-" + secrets.token_hex(6),
        kind=kind,
        display_name=display_name,
        region=region,
        external_id=("cmp-" + secrets.token_hex(16)) if kind in AWS_KINDS else None,
        project_id=project_id,
        expected_principal=expected_principal,
        status="PENDING",
        created_at=_now(),
    )
    session.add(connection)
    session.commit()
    return connection


def get_connection(session: Session, connection_id: str) -> EnvironmentConnection:
    connection = session.get(EnvironmentConnection, connection_id)
    if connection is None:
        raise ConnectionFailure("CONNECTION_NOT_FOUND")
    return connection


def set_role(session: Session, connection_id: str, role_arn: str) -> EnvironmentConnection:
    """Write-once: changing the role means deleting the connection and issuing a new one."""
    connection = get_connection(session, connection_id)
    if connection.kind not in AWS_KINDS:
        raise ConnectionFailure("ROLE_NOT_APPLICABLE")
    if not _ROLE_ARN.fullmatch(role_arn):
        raise ConnectionFailure("INVALID_ROLE_ARN")
    if connection.role_arn is not None:
        if connection.role_arn != role_arn:
            raise ConnectionFailure("ROLE_ALREADY_SET")
        return connection
    connection.role_arn = role_arn
    session.commit()
    return connection


def delete_connection(session: Session, connection_id: str) -> tuple[str, str | None]:
    """Removes platform data only; the role in the user's account stays until they delete it.

    Returns the kind and, for AWS, the stack the user should delete.
    """
    connection = get_connection(session, connection_id)
    kind = connection.kind
    stack = stack_name(connection) if kind in AWS_KINDS else None
    session.delete(connection)
    session.commit()
    return kind, stack


def list_connections(session: Session) -> list[EnvironmentConnection]:
    return list(
        session.scalars(
            select(EnvironmentConnection).order_by(
                EnvironmentConnection.created_at, EnvironmentConnection.id
            )
        )
    )


# ------------------------------------------------------------------------------ AWS


class AwsConnector:
    """Builds STS/EKS clients. The platform side uses the platform principal's named profile."""

    def __init__(self, platform_profile: str | None = None):
        self.platform_profile = platform_profile

    def platform_client(self, service: str, region: str) -> Any:
        session = boto3.Session(profile_name=self.platform_profile, region_name=region)
        return session.client(service, region_name=region, config=_CLIENT_CONFIG)

    def assumed_client(self, service: str, region: str, credentials: dict) -> Any:
        return boto3.client(
            service,
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            config=_CLIENT_CONFIG,
        )


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _assume(connector: AwsConnector, connection: EnvironmentConnection, region: str) -> dict:
    """Returns in-memory STS credentials. Callers must not persist, return or log them."""
    if not connection.role_arn:
        raise ConnectionFailure("ROLE_NOT_SET")
    try:
        response = connector.platform_client("sts", region).assume_role(
            RoleArn=connection.role_arn,
            RoleSessionName=f"cmp-{connection.id}"[:64],
            ExternalId=connection.external_id,
            DurationSeconds=SESSION_SECONDS,
        )
    except ClientError as exc:
        # AWS answers a wrong External ID with the same AccessDenied as a missing trust; the two
        # cannot be told apart from outside, so both are ASSUME_ROLE_DENIED (ADR-005).
        denied = _error_code(exc) in _DENIED
        raise ConnectionFailure("ASSUME_ROLE_DENIED" if denied else "ASSUME_ROLE_FAILED") from None
    except (NoCredentialsError, BotoCoreError):
        raise ConnectionFailure("PLATFORM_CREDENTIALS_UNAVAILABLE") from None
    return response["Credentials"]


def _list_clusters(connector: AwsConnector, credentials: dict, region: str) -> list[str]:
    eks = connector.assumed_client("eks", region, credentials)
    names: list[str] = []
    token = None
    try:
        while True:
            page = eks.list_clusters(**({"nextToken": token} if token else {}))
            names.extend(page.get("clusters", []))
            token = page.get("nextToken")
            if not token:
                return sorted(names)
    except ClientError as exc:
        denied = _error_code(exc) in _DENIED
        raise ConnectionFailure("EKS_LIST_DENIED" if denied else "EKS_LIST_FAILED") from None
    except BotoCoreError:
        raise ConnectionFailure("EKS_LIST_FAILED") from None


def discover_eks_clusters(
    session: Session, connector: AwsConnector, connection_id: str, region: str | None
) -> tuple[str, list[str]]:
    """Anchor discovery: EKS clusters in one region for a source connection, nothing more."""
    connection = get_connection(session, connection_id)
    if connection.kind != "AWS_SOURCE":
        raise ConnectionFailure("SOURCE_CONNECTION_REQUIRED")
    region = region or connection.region
    credentials = _assume(connector, connection, region)
    return region, _list_clusters(connector, credentials, region)


def _verify_aws(connector: AwsConnector, connection: EnvironmentConnection) -> dict:
    credentials = _assume(connector, connection, connection.region)
    try:
        identity = connector.assumed_client(
            "sts", connection.region, credentials
        ).get_caller_identity()
    except (ClientError, BotoCoreError):
        raise ConnectionFailure("IDENTITY_CHECK_FAILED") from None
    account = connection.role_arn.split(":")[4]
    if identity.get("Account") != account:
        raise ConnectionFailure("ACCOUNT_MISMATCH")
    result = {"account_id": account, "principal": identity.get("Arn")}
    if connection.kind == "AWS_SOURCE":
        result["eks_clusters"] = _list_clusters(connector, credentials, connection.region)
    return result


# ------------------------------------------------------------------------------ verify


def verify_connection(
    session: Session,
    connection_id: str,
    *,
    aws: AwsConnector,
    gcp: Callable[[str], str],
) -> dict:
    connection = get_connection(session, connection_id)
    checked_at = _now()
    try:
        if connection.kind in AWS_KINDS:
            result = _verify_aws(aws, connection)
            connection.account_id = result["account_id"]
        else:
            result = {
                "project_id": connection.project_id,
                "principal": gcp(connection.expected_principal),
            }
    except ConnectionFailure as exc:
        connection.status, connection.last_code = "FAILED", exc.code
        connection.last_checked_at = checked_at
        session.commit()
        identity = (
            {"account_id": connection.account_id}
            if connection.kind in AWS_KINDS
            else {"project_id": connection.project_id}
        )
        return {
            "status": "FAILED",
            "code": exc.code,
            "checked_at": checked_at,
            **identity,
            "principal": None,
        }
    connection.status, connection.last_code = "CONNECTED", "OK"
    connection.principal = result["principal"]
    connection.last_checked_at = connection.last_verified_at = checked_at
    session.commit()
    return {"status": "CONNECTED", "code": "OK", "checked_at": checked_at, **result}


# ------------------------------------------------------------------------------ agents


def agent_summaries(session: Session, *, now: int | None = None) -> list[dict]:
    """Operator view of registered Agents plus the last tunnel report (tickets 15/16)."""
    now = _now() if now is None else now
    tunnels = {row.agent_id: row for row in session.scalars(select(TunnelStatus))}
    items = []
    for agent in session.scalars(select(RegisteredAgent).order_by(RegisteredAgent.id)):
        tunnel = tunnels.get(agent.id)
        items.append(
            {
                "agent_id": agent.id,
                "cluster_uid": agent.cluster_uid,
                "last_seen_at": agent.last_seen_at,
                "online": agent.last_seen_at is not None
                and agent.revoked_at is None
                and now - agent.last_seen_at <= ONLINE_SECONDS,
                "revoked": agent.revoked_at is not None,
                "version": None,  # Agent version reporting is a follow-up (ticket 23)
                "tunnel": None
                if tunnel is None
                else {
                    "tunnel_ip": tunnel.tunnel_ip,
                    "last_handshake_at": tunnel.last_handshake_at,
                    "app_health_ok": tunnel.app_health_ok,
                    "db_connect_ok": tunnel.db_connect_ok,
                },
            }
        )
    return items


# ------------------------------------------------------------------------------ credential_ref


def render_aws_config(
    connections: list[EnvironmentConnection], *, base: str, platform_profile: str
) -> str:
    """Shared AWS config: the platform's own profiles, then one assume-role profile per role."""
    parts = [base.rstrip("\n") + "\n"] if base.strip() else []
    for connection in connections:
        ref = credential_ref(connection)
        if ref is None:
            continue
        parts.append(
            f"\n# {connection.kind} connection {connection.id} (generated; do not edit)\n"
            f"[profile {ref}]\n"
            f"role_arn = {connection.role_arn}\n"
            f"external_id = {connection.external_id}\n"
            f"source_profile = {platform_profile}\n"
            f"role_session_name = {ref}\n"
            f"duration_seconds = {PROFILE_SESSION_SECONDS}\n"
            f"region = {connection.region}\n"
        )
    return "".join(parts)


def sync_aws_config(
    session: Session,
    *,
    path: str | None,
    base_path: str | None,
    platform_profile: str | None,
) -> None:
    """Atomically rewrite the generated config from the DB (idempotent, called on each change)."""
    if not path or not platform_profile:
        return
    base = Path(base_path).read_text() if base_path else ""
    content = render_aws_config(
        list_connections(session), base=base, platform_profile=platform_profile
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(temp, 0o640)
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
