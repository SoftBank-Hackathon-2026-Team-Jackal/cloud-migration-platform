"""Provisioning 인계값(`get_migration_handoff`, consumer DATA_MIGRATION) → 대상 `DbEndpoint`(ticket 05).

인계값에는 클러스터·Namespace·DB 릴리스(StatefulSet 이름)가 있다. DB Pod는 `<release>-0`이다.
상대(원본)가 대상 DB로 오는 주소(`peer_host`)는 WireGuard 구성에서 정해지므로 입력으로 받는다.
ON_PREM은 ADR-007에 따라 `tunnel.ingress="source"`와 `target_kubeconfig_ref`를 쓴다.
명시적 데이터 게이트웨이 입력이 없는 기존 경로는 `tunnel_endpoint`를 기본 `peer_host`로 읽는다.
`tunnel`(ticket 10)이 있으면 플랫폼이 두 클러스터 사이에 WireGuard 게이트웨이를 두므로 양쪽 `peer_host`를
게이트웨이 Service 주소로 정한다(입력의 `peer_host`·`target_peer_host`는 쓰지 않는다).
`network_preflight`(Provisioning 76)가 있으면 `preflight_request`가 MNP `NetworkPreflightRequest`를 만들고
데이터 Workflow가 복제 전에 검사한다.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Literal

from cmp_contracts.network_preflight import NetworkPreflightRequest, SecretRef
from pydantic import Field, ValidationError, field_validator, model_validator

from . import tunnel as wg
from .contracts import DbEndpoint, Strict

# k3s 기본 대역. 대상 쪽에서 원본 DB는 게이트웨이 Service(ClusterIP, 10.43/16)로 보인다.
K3S_POD_CIDR, K3S_SERVICE_CIDR = "10.42.0.0/16", "10.43.0.0/16"
TUNNEL_CIDR = "10.210.0.0/24"  # tunnel.ADDRESSES(게이트웨이 WireGuard 주소)


class NetworkPreflightSettings(Strict):
    """`data_migration.network_preflight`(Provisioning 76, 운영자 값, 비밀값 없음). 있으면 복제 전에
    MNP 검사를 하고 같은 입력의 PASS만 통과한다. 주소·검사 위치는 인계값과 `tunnel`에서 정한다."""

    aws_vpc_cidrs: list[str] = Field(min_length=1)  # 원본 EKS VPC
    onprem_routed_cidrs: list[str] = Field(default=[K3S_POD_CIDR, K3S_SERVICE_CIDR], min_length=1)
    tunnel_cidrs: list[str] = Field(default=[TUNNEL_CIDR])
    allowed_private_destination_cidrs: list[str] = Field(default=[K3S_SERVICE_CIDR], min_length=1)
    # WireGuard가 이미 암호화하고, 복제 구독도 sslmode를 정하지 않는다(원본 기본 PostgreSQL은 TLS 없음)
    tls_mode: Literal["disable", "require", "verify-full"] = "disable"
    # 검사 Pod가 읽는 Secret(검사 Namespace에 운영자가 둔다). 복제 역할 Secret은 구독 단계에서야 생겨 못 쓴다
    db_auth_ref: SecretRef
    probe_namespace: str | None = None  # 기본: 대상 DB Namespace(복제가 도는 곳)
    node_name: str | None = None  # 기본: 지정 안 함(단일 노드 k3d)
    transfer_bytes: int | None = None
    timeout_seconds: int | None = None

    @field_validator(
        "aws_vpc_cidrs", "onprem_routed_cidrs", "tunnel_cidrs", "allowed_private_destination_cidrs"
    )
    @classmethod
    def _cidrs(cls, values: list[str]) -> list[str]:
        # 시작 때 거른다(MNP 요청과 같은 규칙: IPv4, 호스트 비트 없음)
        return [str(ipaddress.IPv4Network(value, strict=True)) for value in values]


class DataMigrationInput(Strict):
    """마이그레이션 입력의 `data_migration` 항목(운영자가 주는 값, 비밀값 없음)."""

    source: DbEndpoint
    app: dict[str, Any]  # cutover.SourceApp
    settings: dict[str, Any]  # cutover.CutoverSettings
    target_peer_host: str | None = None
    target_peer_port: int = Field(default=5432, ge=1, le=65535)
    # 대상 클러스터에 Worker가 kubeconfig로 붙어야 할 때(ON_PREM 등)
    target_kubeconfig_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+$")
    sample_interval_s: float = Field(default=2, gt=0)
    monitor_interval_s: float = Field(default=5, gt=0)
    # ticket 10: 플랫폼이 두 클러스터 사이 WireGuard 경로를 만든다
    tunnel: wg.WireGuardTunnel | None = None
    # Provisioning 76: 복제 전 네트워크 사전 점검(MNP). `tunnel`이 있어야 한다(검사 주소가 게이트웨이다)
    network_preflight: NetworkPreflightSettings | None = None

    @model_validator(mode="before")
    @classmethod
    def _tunnel_peer(cls, data: Any) -> Any:
        """`tunnel`이 있으면 원본 `peer_host`는 플랫폼이 정하므로 생략할 수 있다."""
        if not isinstance(data, dict) or not data.get("tunnel"):
            return data
        source = data.get("source")
        if isinstance(source, dict) and not source.get("peer_host"):
            data = {**data, "source": {**source, "peer_host": "tunnel"}}
        return data

    @model_validator(mode="after")
    def _tunnel_local(self) -> DataMigrationInput:
        if self.tunnel and not self.source.local_host:
            raise ValueError("tunnel needs source.local_host (the source DB Service address)")
        if self.network_preflight and not self.tunnel:
            raise ValueError("network_preflight needs tunnel (it probes the gateway address)")
        return self


def preflight_request(
    view: dict[str, Any], given: DataMigrationInput, migration_id: str, target: DbEndpoint
) -> tuple[dict[str, Any] | None, str | None]:
    """(MNP 요청 JSON, None), 설정이 없으면 (None, None), 만들 수 없으면 (None, 막힌 이유 코드).

    원본 주소는 대상 쪽에서 본 게이트웨이 Service다. 검사 위치는 인계값의 Agent·클러스터와 대상 DB
    Namespace다. 실행기 선택은 `run_network_preflight`가 한다(Agent 경로 또는 Worker kubeconfig)."""
    settings = given.network_preflight
    if settings is None:
        return None, None
    cluster = (view.get("common") or {}).get("cluster") or {}
    if not cluster.get("agent_id") or not cluster.get("cluster_uid"):
        return None, "NETWORK_PREFLIGHT_TARGET_MISSING"
    probe = {"agent_id": cluster["agent_id"], "cluster_uid": cluster["cluster_uid"],
             "namespace": settings.probe_namespace or target.namespace}  # fmt: skip
    if settings.node_name:
        probe["node_name"] = settings.node_name
    limits = {k: v for k, v in (("transfer_bytes", settings.transfer_bytes),
                                ("timeout_seconds", settings.timeout_seconds)) if v is not None}  # fmt: skip
    try:
        request = NetworkPreflightRequest(
            migration_id=migration_id,
            source_db_host=wg.peer_host(migration_id),
            source_db_port=wg.DB_PORT,
            tls_mode=settings.tls_mode,
            database=given.source.dbname,
            db_auth_ref=settings.db_auth_ref,
            aws_vpc_cidrs=settings.aws_vpc_cidrs,
            onprem_routed_cidrs=settings.onprem_routed_cidrs,
            tunnel_cidrs=settings.tunnel_cidrs,
            expected_network_path={
                "allowed_private_destination_cidrs": settings.allowed_private_destination_cidrs
            },
            probe_target=probe,
            **limits,
        )
    except ValidationError:
        return None, "NETWORK_PREFLIGHT_INPUT_INVALID"
    return request.model_dump(mode="json"), None


def endpoints(
    view: dict[str, Any], given: DataMigrationInput, migration_id: str
) -> tuple[DbEndpoint | None, DbEndpoint | None, str | None]:
    """(원본, 대상, None) 또는 (None, None, 막힌 이유 코드). `tunnel`이 있으면 양쪽 `peer_host`를 게이트웨이
    Service로 바꾸고, 대상 `local_host`(대상 게이트웨이가 대상 DB로 가는 주소)를 DB Service로 정한다."""
    if given.tunnel is None:
        target, code = target_endpoint(view, given)
        return (given.source if target else None), target, code
    host = wg.peer_host(migration_id)
    peer = given.model_copy(update={"target_peer_host": host, "target_peer_port": wg.DB_PORT})
    target, code = target_endpoint(view, peer)
    if target is None:
        return None, None, code
    release = (view.get("database") or {}).get("release")
    target = target.model_copy(
        update={"local_host": f"{release}.{target.namespace}.svc.cluster.local"}
    )
    source = given.source.model_copy(update={"peer_host": host, "peer_port": wg.DB_PORT})
    return source, target, None


def target_endpoint(
    view: dict[str, Any], given: DataMigrationInput
) -> tuple[DbEndpoint | None, str | None]:
    """(대상 DbEndpoint, None) 또는 (None, 막힌 이유 코드)."""
    if not view.get("proceed", False):
        blocked = view.get("blocked_by") or ["HANDOFF_NOT_READY"]
        return None, str(blocked[0])
    common, db = view.get("common") or {}, view.get("database") or {}
    cluster = common.get("cluster") or {}
    namespace, release = common.get("namespace"), db.get("release")
    if not namespace or not release or not db.get("name"):
        return None, "HANDOFF_DATABASE_INCOMPLETE"
    peer_host, peer_port = given.target_peer_host, given.target_peer_port
    tunnel = db.get("tunnel_endpoint")
    if not peer_host and tunnel:
        host, _, port = str(tunnel).rpartition(":")
        peer_host, peer_port = host, int(port or 5432)
    if not peer_host:
        return None, "TARGET_PEER_HOST_MISSING"  # WireGuard 쪽 주소가 정해지지 않음
    base = {
        "namespace": namespace,
        "pod": f"{release}-0",
        "peer_host": peer_host,
        "peer_port": peer_port,
        "dbname": db["name"],
    }
    if given.target_kubeconfig_ref:
        return DbEndpoint(
            access="KUBECONFIG", kubeconfig_ref=given.target_kubeconfig_ref, **base
        ), None
    kind = cluster.get("kind")
    if kind == "EKS":
        return DbEndpoint(
            access="AWS_EKS",
            cluster_name=cluster.get("name"),
            region=cluster.get("location"),
            **base,
        ), None
    if kind == "GKE":
        return DbEndpoint(
            access="GCP_GKE",
            cluster_name=cluster.get("name"),
            location=cluster.get("location"),
            project_id=cluster.get("account_id"),
            **base,
        ), None
    return None, "TARGET_DB_ACCESS_UNSUPPORTED"  # K3s는 target_kubeconfig_ref가 있어야 한다
