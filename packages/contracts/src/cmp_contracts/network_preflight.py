"""Migration Network Preflight contracts (MNP-001~006, decisions N1~N8).

The request names the real replication path: source DB endpoint, TLS mode, a Secret *reference*,
routed CIDRs, the expected tunnel path and where the probe runs. Results hold codes and small
diagnostics only; never credentials, query payloads or tunnel keys.
"""

from datetime import datetime
from ipaddress import IPv4Network
from typing import Annotated, Literal

from pydantic import Field, field_validator

from cmp_contracts.provisioning import Contract, Text

CheckStatus = Literal["PASS", "FAIL", "UNKNOWN"]
Host = Annotated[str, Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")]
K8sName = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")]
SecretKey = Annotated[str, Field(pattern=r"^[-._A-Za-z0-9]{1,253}$")]
Interface = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,15}$")]
MiB = 1024 * 1024


def _cidrs(values: list[str]) -> list[str]:
    # IPv4 only in P0; normalise so equal ranges compare equal in the fingerprint.
    return [str(IPv4Network(value, strict=True)) for value in values]


class SecretRef(Contract):
    secret_name: K8sName
    username_key: SecretKey
    password_key: SecretKey


class ExpectedPath(Contract):
    allowed_private_destination_cidrs: list[str] = Field(min_length=1)
    tunnel_interface: Interface | None = None  # e.g. wg0 on the probe node
    tunnel_gateway: str | None = None  # optional next hop expected on that interface

    @field_validator("allowed_private_destination_cidrs")
    @classmethod
    def _normalise(cls, values: list[str]) -> list[str]:
        return _cidrs(values)


class ProbeTarget(Contract):
    agent_id: K8sName
    cluster_uid: Text
    namespace: K8sName
    node_name: K8sName | None = None  # the node the replication workload runs on


class NetworkPreflightRequest(Contract):
    contract_version: Literal["1"] = "1"
    migration_id: Text
    source_db_host: Host
    source_db_port: int = Field(ge=1, le=65535)
    tls_mode: Literal["disable", "require", "verify-full"]
    database: Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{1,63}$")]
    db_auth_ref: SecretRef
    aws_vpc_cidrs: list[str]
    onprem_routed_cidrs: list[str]
    tunnel_cidrs: list[str]
    expected_network_path: ExpectedPath
    probe_target: ProbeTarget
    transfer_bytes: int = Field(default=4 * MiB, ge=64 * 1024, le=64 * MiB)
    timeout_seconds: int = Field(default=60, ge=5, le=600)

    @field_validator("aws_vpc_cidrs", "onprem_routed_cidrs", "tunnel_cidrs")
    @classmethod
    def _normalise(cls, values: list[str]) -> list[str]:
        return _cidrs(values)


class NodeRoute(Contract):
    dev: str | None
    via: str | None
    src: str | None


class Transfer(Contract):
    bytes: int = Field(ge=0)
    seconds: float = Field(ge=0)
    status: Literal["COMPLETE", "INTERRUPTED", "TIMEOUT"]


class ProbeObservation(Contract):
    """What the on-site executor saw. Never contains query output or credentials."""

    node: str | None
    resolved_ip: str | None
    node_route: NodeRoute | None  # None: node routing could not be observed
    db_stage_failed: (
        Literal["DNS_FAILED", "TCP_UNREACHABLE", "TLS_FAILED", "AUTH_FAILED", "QUERY_FAILED"] | None
    )
    transfer: Transfer | None
    mtu: dict[str, str] | None = None


class PreflightCheck(Contract):
    name: Literal["check_cidr_overlap", "check_route", "probe_db_path", "probe_large_transfer"]
    status: CheckStatus
    code: str | None = None
    evidence: dict = Field(default_factory=dict)


class ProbeLocation(Contract):
    agent_id: str
    cluster_uid: str
    namespace: str
    node: str | None


class NetworkPreflightResult(Contract):
    contract_version: Literal["1"] = "1"
    status: CheckStatus
    checks: list[PreflightCheck]
    migration_id: str
    tested_endpoint: str
    probe_location: ProbeLocation
    checked_at: datetime
    input_fingerprint: str
