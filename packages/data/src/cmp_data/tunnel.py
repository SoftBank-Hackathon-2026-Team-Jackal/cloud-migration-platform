"""원본·대상 클러스터 사이 WireGuard 복제 경로(DM-011, ticket 10). 값과 매니페스트만 만든다(입출력 없음).

ticket 06 실습(`infra/data-migration/crosscloud-lab`)과 같은 구성이다. 각 클러스터의 `cmp-data-tunnel`
Namespace에 게이트웨이 Pod 하나를 둔다.

```
원본 클러스터                                         대상 클러스터
 DB ◀─ gateway 10.210.0.1 ──── WireGuard UDP ────▶ LB ─ gateway 10.210.0.2 ─▶ DB
       Service <name>-peer-db:5432 → 터널 → 대상 DB    Service <name>-peer-db:5432 → 터널 → 원본 DB
       (게이트웨이 Pod 포트도 5432: 대상 DB 송신 정책이 5432만 허용한다)
```

- 기본 입구는 대상 LoadBalancer(UDP)다. 원본 NAT 공인 IP(`source_egress_cidrs`)만 받는다.
- `ingress="source"`는 원본 EKS의 공개 NLB를 쓰고 대상 NAT 공인 IP(`target_egress_cidrs`)만 받는다.
  대상 게이트웨이가 접속하며 keepalive 25초, 원본 Pod의 TCP 8080으로 NLB 상태를 확인한다.
- `standing_ingress`(Provisioning 82)는 원본 NLB Service를 고정 이름(`cmp-wg-standing-endpoint`)으로 재사용한다.
  NLB 생성(2~3분)을 실행 전에 끝내 두고, 실행마다 selector(이번 게이트웨이)와 허용 IP만 바꾼다. 정리 때
  이 Service는 지우지 않는다. 게이트웨이·키·정책은 지금처럼 실행마다 새로 만든다. 한 번에 한 실행만 쓴다.
- 양쪽 Service 이름이 같아서, 원본·대상 `peer_host`가 모두 `<name>-peer-db.cmp-data-tunnel.svc.cluster.local`이다.
  원본 DB에서 보면 대상 DB, 대상 DB에서 보면 원본 DB로 이어진다.
- 대상 Namespace에는 Provisioning의 기본 차단 정책이 있다. 게이트웨이를 별도 Namespace에 두고, 대상 DB
  5432로 게이트웨이만 들어오도록 허용 정책 하나를 더한다(NetworkPolicy는 허용만 더해진다).
- WireGuard 비밀키는 각 클러스터 Secret에만 있다. 매니페스트·Workflow 입력/결과·로그에는 공개키만 쓴다.
"""

from __future__ import annotations

import hashlib
import ipaddress
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import Strict

NAMESPACE = "cmp-data-tunnel"
WG_PORT = 51820
DB_PORT = 5432
HEALTH_PORT = 8080
# 게이트웨이의 두 리스너는 모두 5432다. 터널 쪽(WG_IP)은 이 클러스터 DB로, Pod 주소 쪽(POD_IP)은 터널 너머
# DB로 넘긴다. Pod 포트를 5432로 두어야 대상 DB의 Provisioning 정책(송신은 TCP 5432만)을 통과한다.
ADDRESSES = {"source": "10.210.0.1", "target": "10.210.0.2"}
LABELS = {"app.kubernetes.io/managed-by": "cmp-data-migration"}
SECRET_KEY = "private"
# Provisioning 82: 원본 클러스터의 cmp-data-tunnel Namespace에 하나만 둔다. 미리 만들 때는 아무 Pod도 고르지 않는다.
STANDING_ENDPOINT = "cmp-wg-standing-endpoint"
STANDING_IDLE = "standing-idle"
Side = Literal["source", "target"]

# Provisioning 81: 컨테이너가 재시작하면 같은 Pod 네트워크에 이전 wg0이 남는다(먼저 지운다). 막 생긴 NLB의
# DNS 이름은 몇 분 뒤에 풀리므로 endpoint는 풀릴 때까지 다시 시도한다. 상한은 dm_tunnel의 handshake 대기다.
START_SCRIPT = """set -eu
apk add --no-cache wireguard-tools wireguard-go socat iproute2 >/dev/null
ip link del wg0 2>/dev/null || true
if ! ip link add wg0 type wireguard 2>/dev/null; then
  mkdir -p /dev/net; [ -c /dev/net/tun ] || mknod /dev/net/tun c 10 200
  WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 wireguard-go wg0
fi
wg set wg0 private-key /etc/wg/private listen-port 51820 \\
  peer "$PEER_PUBLIC_KEY" allowed-ips "$PEER_IP/32" persistent-keepalive "${KEEPALIVE:-15}"
ip addr add "$WG_IP/24" dev wg0
ip link set mtu 1420 up dev wg0
socat TCP-LISTEN:5432,bind="$WG_IP",fork,reuseaddr TCP:"$LOCAL_DB":5432 &
socat TCP-LISTEN:5432,bind="$POD_IP",fork,reuseaddr TCP:"$PEER_IP":5432 &
if [ -n "${HEALTH_PORT:-}" ]; then
  socat TCP-LISTEN:"$HEALTH_PORT",bind="$POD_IP",fork,reuseaddr SYSTEM:true &
fi
if [ -n "${PEER_ENDPOINT:-}" ]; then
  (until wg set wg0 peer "$PEER_PUBLIC_KEY" endpoint "$PEER_ENDPOINT"; do
    echo "PEER_ENDPOINT_UNRESOLVED $PEER_ENDPOINT; retrying in 5s" >&2
    sleep 5
  done) &
fi
touch /tmp/started
wait
"""


class WireGuardTunnel(Strict):
    """마이그레이션 입력의 `data_migration.tunnel`(운영자 값, 비밀값 없음)."""

    ingress: Side = "target"
    # 접속하는 쪽의 실제 송신 공인 IP(NAT). 선택한 입구 반대편의 CIDR만 받는다.
    source_egress_cidrs: list[str] | None = Field(default=None, min_length=1, max_length=8)
    target_egress_cidrs: list[str] | None = Field(default=None, min_length=1, max_length=8)
    image: str = "docker.io/library/alpine:3.22"
    # LB 주소 대기. source 입구는 이 예산 안에서 NLB healthy/handshake도 기다린다.
    endpoint_timeout_s: int = Field(default=600, ge=30, le=1800)
    handshake_timeout_s: int = Field(default=180, ge=10, le=900)
    # Provisioning 82: 원본 NLB를 고정 이름으로 재사용한다(ingress="source"만).
    standing_ingress: bool = False

    @field_validator("source_egress_cidrs", "target_egress_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        out = []
        for cidr in value:
            net = ipaddress.ip_network(cidr, strict=True)
            if net.version != 4 or net.prefixlen < 16:
                raise ValueError("egress CIDRs must be IPv4 /16 or narrower")
            out.append(str(net))
        return out

    @model_validator(mode="after")
    def _ingress_cidrs(self) -> WireGuardTunnel:
        required = "target_egress_cidrs" if self.ingress == "source" else "source_egress_cidrs"
        forbidden = "source_egress_cidrs" if self.ingress == "source" else "target_egress_cidrs"
        if not getattr(self, required) or getattr(self, forbidden) is not None:
            raise ValueError(f"ingress={self.ingress} requires {required} and forbids {forbidden}")
        if self.standing_ingress and self.ingress != "source":
            raise ValueError("standing_ingress requires ingress=source")
        return self

    @property
    def endpoint_service(self) -> str | None:
        """고정 입구 Service 이름. 실행별 입구면 None(`<name>-endpoint`)."""
        return STANDING_ENDPOINT if self.standing_ingress else None

    @property
    def egress_cidrs(self) -> list[str]:
        return (
            self.target_egress_cidrs if self.ingress == "source" else self.source_egress_cidrs
        ) or []


def name(migration_id: str) -> str:
    """마이그레이션마다 다른 짧은 이름(같은 클러스터에서 둘이 겹치지 않게)."""
    return "cmp-wg-" + hashlib.sha256(migration_id.encode()).hexdigest()[:8]


def peer_host(migration_id: str) -> str:
    return f"{name(migration_id)}-peer-db.{NAMESPACE}.svc.cluster.local"


def _meta(obj_name: str, migration_id: str) -> dict[str, Any]:
    return {
        "name": obj_name,
        "namespace": NAMESPACE,
        "labels": {**LABELS, "cmp.dev/data-tunnel": name(migration_id)},
    }


def namespace_manifest() -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": NAMESPACE, "labels": dict(LABELS)}}  # fmt: skip


def gateway_manifests(
    migration_id: str,
    side: Side,
    *,
    image: str,
    local_db: str,
    peer_public_key: str,
    peer_endpoint: str | None,
    ingress: Side = "target",
) -> list[dict[str, Any]]:
    """ConfigMap(시작 스크립트), Deployment(게이트웨이), Service(`<name>-peer-db`). Secret은 따로 만든다."""
    base = name(migration_id)
    other: Side = "target" if side == "source" else "source"
    selector = {"cmp.dev/data-tunnel": base}
    env = {
        "WG_IP": ADDRESSES[side],
        "PEER_IP": ADDRESSES[other],
        "PEER_PUBLIC_KEY": peer_public_key,
        "PEER_ENDPOINT": peer_endpoint or "",
        "LOCAL_DB": local_db,
    }
    if ingress == "source":
        env["KEEPALIVE"] = "25"
        if side == ingress:
            env["HEALTH_PORT"] = str(HEALTH_PORT)
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta(base, migration_id),
              "data": {"start.sh": START_SCRIPT}}  # fmt: skip
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _meta(base, migration_id),
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": {**LABELS, **selector}},
                "spec": {
                    "containers": [{
                        "name": "wg",
                        "image": image,
                        "command": ["sh", "/opt/start.sh"],
                        "env": [
                            *({"name": k, "value": v} for k, v in env.items()),
                            {"name": "POD_IP",
                             "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
                        ],
                        "ports": [
                            {"name": "wg", "containerPort": WG_PORT, "protocol": "UDP"},
                            {"name": "peer-db", "containerPort": DB_PORT},
                        ],
                        # 커널 WireGuard·tun 장치가 필요하다(ticket 06 실습과 같음)
                        "securityContext": {"privileged": True},
                        # handshake를 조건으로 두면 LB가 이 Pod로 UDP를 보내지 않아 터널이 서지 않는다
                        "readinessProbe": {"exec": {"command": ["test", "-f", "/tmp/started"]},
                                           "periodSeconds": 3},
                        "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
                        "volumeMounts": [
                            {"name": "start", "mountPath": "/opt"},
                            {"name": "key", "mountPath": "/etc/wg", "readOnly": True},
                        ],
                    }],
                    "volumes": [
                        {"name": "start", "configMap": {"name": base}},
                        {"name": "key", "secret": {"secretName": base}},
                    ],
                },
            },
        },
    }  # fmt: skip
    if ingress == "source" and side == ingress:
        deployment["spec"]["template"]["spec"]["containers"][0]["ports"].append(
            {"name": "health", "containerPort": HEALTH_PORT}
        )
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(f"{base}-peer-db", migration_id),
        "spec": {"selector": selector,
                 "ports": [{"name": "postgres", "port": DB_PORT, "targetPort": DB_PORT}]},
    }  # fmt: skip
    return [config, deployment, service]


def endpoint_manifest(
    migration_id: str,
    source_egress_cidrs: list[str],
    *,
    ingress: Side = "target",
    standing: bool = False,
) -> dict[str, Any]:
    """선택한 쪽의 UDP 입구. CIDR 인자는 접속하는 반대편의 송신 공인 IP다.

    기존 위치/키워드 인자 이름은 호환성을 위해 보존한다. `standing`이면 고정 이름이고 실행별 라벨이 없다.
    """
    base = name(migration_id)
    if standing:
        metadata = {"name": STANDING_ENDPOINT, "namespace": NAMESPACE,
                    "labels": {**LABELS, "cmp.dev/data-tunnel-standing": "true"}}  # fmt: skip
    else:
        metadata = _meta(f"{base}-endpoint", migration_id)
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata,
        "spec": {
            "type": "LoadBalancer",
            "loadBalancerSourceRanges": list(source_egress_cidrs),
            "selector": {"cmp.dev/data-tunnel": base},
            "ports": [{"name": "wg", "port": WG_PORT, "targetPort": WG_PORT, "protocol": "UDP"}],
        },
    }

    if ingress == "source":
        prefix = "service.beta.kubernetes.io/aws-load-balancer-"
        service["metadata"]["annotations"] = {
            f"{prefix}scheme": "internet-facing",
            f"{prefix}nlb-target-type": "ip",
            f"{prefix}attributes": "load_balancing.cross_zone.enabled=true",
            f"{prefix}healthcheck-protocol": "tcp",
            f"{prefix}healthcheck-port": str(HEALTH_PORT),
            # Provisioning 82: 새 게이트웨이 Pod가 대상으로 등록된 뒤 healthy까지 기본 30초 → 약 10초.
            # NLB 허용 범위: 간격 5~300초, 제한 시간 2~120초, 임계값 2~10.
            f"{prefix}healthcheck-interval": "5",
            f"{prefix}healthcheck-timeout": "4",
            f"{prefix}healthcheck-healthy-threshold": "2",
            f"{prefix}healthcheck-unhealthy-threshold": "2",
        }
        service["spec"]["loadBalancerClass"] = "service.k8s.aws/nlb"
    return service


def standing_endpoint_manifest(target_egress_cidrs: list[str]) -> dict[str, Any]:
    """시연 전에 미리 만드는 원본 NLB. 아무 게이트웨이도 고르지 않고, 실행이 selector를 바꾼다."""
    service = endpoint_manifest(STANDING_IDLE, target_egress_cidrs, ingress="source", standing=True)
    service["spec"]["selector"] = {"cmp.dev/data-tunnel": STANDING_IDLE}
    return service


def db_ingress_policy(migration_id: str, db_namespace: str, db_release: str) -> dict[str, Any]:
    """대상 DB Namespace(Provisioning 기본 차단)에서 게이트웨이 → DB 5432만 허용한다."""
    base = name(migration_id)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": f"{base}-db-in", "namespace": db_namespace,
                     "labels": {**LABELS, "cmp.dev/data-tunnel": base}},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/instance": db_release}},
            "policyTypes": ["Ingress"],
            "ingress": [{
                "from": [{
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}},
                    "podSelector": {"matchLabels": {"cmp.dev/data-tunnel": base}},
                }],
                "ports": [{"protocol": "TCP", "port": DB_PORT}],
            }],
        },
    }  # fmt: skip
