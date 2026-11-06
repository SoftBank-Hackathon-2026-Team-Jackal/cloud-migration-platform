"""Provisioning 인계값(`get_migration_handoff`, consumer DATA_MIGRATION) → 대상 `DbEndpoint`(ticket 05).

인계값에는 클러스터·Namespace·DB 릴리스(StatefulSet 이름)가 있다. DB Pod는 `<release>-0`이다.
상대(원본)가 대상 DB로 오는 주소(`peer_host`)는 WireGuard 구성에서 정해지므로 입력으로 받는다.
ON_PREM은 터널 쪽 DB 주소(`tunnel_endpoint`)를 기본 `peer_host`로 쓴다.
`tunnel`(ticket 10)이 있으면 플랫폼이 두 클러스터 사이에 WireGuard 게이트웨이를 두므로 양쪽 `peer_host`를
게이트웨이 Service 주소로 정한다(입력의 `peer_host`·`target_peer_host`는 쓰지 않는다).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from . import tunnel as wg
from .contracts import DbEndpoint, Strict


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
        return self


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
