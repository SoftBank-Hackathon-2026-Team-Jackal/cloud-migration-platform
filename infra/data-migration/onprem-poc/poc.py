"""2안 실측 PoC 매니페스트(티켓 69). 표준 라이브러리만 쓴다. 비밀키는 다루지 않는다(up.sh가 stdin으로 Secret에).

`cmp_data.tunnel`(입구=대상 LB, 원본이 접속)을 뒤집었다. 입구는 원본 EKS의 UDP NLB, NAT 뒤 k3d가 밖으로 접속한다.

```
원본 EKS (ns cmp-dm-poc)                                맥 k3d (ns cmp-dm-poc, NAT 뒤)
 chat-db ◀─ socat ◀─ gw 10.211.0.1 ◀─ NLB UDP 51820 ◀─── gw 10.211.0.2 (endpoint=NLB, keepalive 25)
                                                         Service src-db:5432 → 터널 → 원본 chat-db
```

`python poc.py eks|k3d ...`가 kubectl용 JSON List를 stdout에 낸다.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from typing import Any

NAMESPACE = "cmp-dm-poc"
LABELS = {"app.kubernetes.io/managed-by": "cmp-onprem-poc"}
IMAGE = "docker.io/library/alpine:3.22"  # amd64·arm64 둘 다 있다
WG_PORT = 51820
DB_PORT = 5432
HEALTH_PORT = 8080  # NLB는 UDP 헬스체크를 못 한다. 게이트웨이의 TCP 포트로 본다
# 데이터 10.210(DM-011)·중계 10.200(ADR-006)과 겹치지 않게
ADDRESSES = {"eks": "10.211.0.1", "k3d": "10.211.0.2"}
SOURCE_DB = "chat-db-0.chat-db.chat-source.svc.cluster.local"
KEEPALIVE = 25
NAME = "wg-gateway"

START_SCRIPT = """set -eu
apk add --no-cache wireguard-tools wireguard-go socat iproute2 >/dev/null
if ! ip link add wg0 type wireguard 2>/dev/null; then
  mkdir -p /dev/net; [ -c /dev/net/tun ] || mknod /dev/net/tun c 10 200
  WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 wireguard-go wg0
  echo "wireguard: userspace"
else
  echo "wireguard: kernel"
fi
wg set wg0 private-key /etc/wg/private listen-port 51820 \\
  peer "$PEER_PUBLIC_KEY" allowed-ips "$PEER_IP/32" persistent-keepalive "$KEEPALIVE" \\
  ${PEER_ENDPOINT:+endpoint "$PEER_ENDPOINT"}
ip addr add "$WG_IP/24" dev wg0
ip link set mtu 1420 up dev wg0
if [ -n "$LOCAL_DB" ]; then
  # 터널로 온 5432 → 이 클러스터 DB, NLB 헬스체크용 TCP 포트
  socat TCP-LISTEN:5432,bind="$WG_IP",fork,reuseaddr TCP:"$LOCAL_DB":5432 &
  socat TCP-LISTEN:8080,fork,reuseaddr SYSTEM:true &
else
  # Pod 주소 5432 → 터널 너머 DB
  socat TCP-LISTEN:5432,bind="$POD_IP",fork,reuseaddr TCP:"$PEER_IP":5432 &
fi
touch /tmp/started
wait
"""


def _meta(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "namespace": NAMESPACE, "labels": dict(LABELS), **extra}


def _cidrs(values: list[str]) -> list[str]:
    out = []
    for value in values:
        net = ipaddress.ip_network(value, strict=True)
        if net.version != 4 or net.prefixlen < 24:
            raise ValueError(f"source CIDR must be IPv4 /24 or narrower: {value}")
        out.append(str(net))
    if not out:
        raise ValueError("at least one source CIDR")
    return out


def _gateway(
    side: str, *, peer_public_key: str, endpoint: str, local_db: str
) -> list[dict[str, Any]]:
    other = "k3d" if side == "eks" else "eks"
    env = {
        "WG_IP": ADDRESSES[side],
        "PEER_IP": ADDRESSES[other],
        "PEER_PUBLIC_KEY": peer_public_key,
        "PEER_ENDPOINT": endpoint,
        "KEEPALIVE": str(KEEPALIVE),
        "LOCAL_DB": local_db,
    }
    ports = [{"name": "wg", "containerPort": WG_PORT, "protocol": "UDP"}]
    ports.append({"name": "health", "containerPort": HEALTH_PORT} if local_db
                 else {"name": "db", "containerPort": DB_PORT})  # fmt: skip
    selector = {"app": NAME}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta(NAME),
              "data": {"start.sh": START_SCRIPT}}  # fmt: skip
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _meta(NAME),
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": {**LABELS, **selector}},
                "spec": {
                    "containers": [{
                        "name": "wg",
                        "image": IMAGE,
                        "command": ["sh", "/opt/start.sh"],
                        "env": [
                            *({"name": k, "value": v} for k, v in env.items()),
                            {"name": "POD_IP",
                             "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
                        ],
                        "ports": ports,
                        "securityContext": {"privileged": True},
                        "readinessProbe": {"exec": {"command": ["test", "-f", "/tmp/started"]},
                                           "periodSeconds": 3},
                        "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
                        "volumeMounts": [
                            {"name": "start", "mountPath": "/opt"},
                            {"name": "key", "mountPath": "/etc/wg", "readOnly": True},
                        ],
                    }],
                    "volumes": [
                        {"name": "start", "configMap": {"name": NAME}},
                        {"name": "key", "secret": {"secretName": "wg-key"}},
                    ],
                },
            },
        },
    }  # fmt: skip
    return [config, deployment]


def _namespace() -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": NAMESPACE, "labels": dict(LABELS)}}  # fmt: skip


def eks_manifests(
    *, peer_public_key: str, source_cidrs: list[str], local_db: str = SOURCE_DB
) -> list[dict[str, Any]]:
    """원본 EKS: 게이트웨이(기다리기만) + 인터넷용 UDP NLB(허용 CIDR만)."""
    cidrs = _cidrs(source_cidrs)
    prefix = "service.beta.kubernetes.io/aws-load-balancer-"
    endpoint = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta("wg-endpoint", annotations={
            f"{prefix}scheme": "internet-facing",
            f"{prefix}nlb-target-type": "ip",
            f"{prefix}healthcheck-protocol": "TCP",
            f"{prefix}healthcheck-port": str(HEALTH_PORT),
            f"{prefix}additional-resource-tags": "cmp.dev/purpose=onprem-poc,cmp.dev/ticket=69",
        }),
        "spec": {
            "type": "LoadBalancer",
            "loadBalancerClass": "service.k8s.aws/nlb",
            "loadBalancerSourceRanges": cidrs,
            "selector": {"app": NAME},
            "ports": [{"name": "wg", "port": WG_PORT, "targetPort": WG_PORT, "protocol": "UDP"}],
        },
    }  # fmt: skip
    gateway = _gateway("eks", peer_public_key=peer_public_key, endpoint="", local_db=local_db)
    return [_namespace(), *gateway, endpoint]


def k3d_manifests(*, peer_public_key: str, endpoint: str) -> list[dict[str, Any]]:
    """맥 k3d: NLB로 접속하는 게이트웨이 + 원본 DB로 이어지는 Service `src-db`."""
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta("src-db"),
        "spec": {"selector": {"app": NAME},
                 "ports": [{"name": "postgres", "port": DB_PORT, "targetPort": DB_PORT}]},
    }  # fmt: skip
    gateway = _gateway("k3d", peer_public_key=peer_public_key, endpoint=endpoint, local_db="")
    return [_namespace(), *gateway, service]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="side", required=True)
    eks = sub.add_parser("eks")
    eks.add_argument("--peer-public-key", required=True)
    eks.add_argument("--source-cidr", action="append", required=True)
    k3d = sub.add_parser("k3d")
    k3d.add_argument("--peer-public-key", required=True)
    k3d.add_argument("--endpoint", required=True)
    args = parser.parse_args(argv)
    if args.side == "eks":
        items = eks_manifests(peer_public_key=args.peer_public_key, source_cidrs=args.source_cidr)
    else:
        items = k3d_manifests(peer_public_key=args.peer_public_key, endpoint=args.endpoint)
    json.dump({"apiVersion": "v1", "kind": "List", "items": items}, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
