"""중계 서버 배포 자산 생성(#63): Envoy bootstrap과 WireGuard 설정 템플릿.

비밀값(개인키 등)은 파일에 쓰지 않고 자리표시자로만 둔다. 실제 키는 배포 때 주입한다.
관리 포트(9901)는 루프백에만 연다(공개하지 않음).
"""

from __future__ import annotations

import json
import re
import time

import yaml

_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")
_LINE_SAFE = re.compile(r"^[A-Za-z0-9./:,\-\[\]]+$")


def _host(value: str) -> str:
    if not _HOST.fullmatch(value):
        raise ValueError(f"허용하지 않는 호스트 값: {value!r}")
    return value


def _cluster(name: str, host: str, port: int, health_path: str) -> dict:
    return {
        "name": name,
        "type": "STRICT_DNS",
        "connect_timeout": "3s",  # WAN 구간 기준(실측 후 조정)
        "health_checks": [
            {
                "timeout": "1s",
                "interval": "2s",
                "unhealthy_threshold": 2,
                "healthy_threshold": 2,
                "http_health_check": {"path": health_path},
            }
        ],
        "load_assignment": {
            "cluster_name": name,
            "endpoints": [
                {
                    "lb_endpoints": [
                        {
                            "endpoint": {
                                "address": {
                                    "socket_address": {"address": _host(host), "port_value": port}
                                }
                            }
                        }
                    ]
                }
            ],
        },
    }


CLUSTER_TYPE = "type.googleapis.com/envoy.config.cluster.v3.Cluster"


def render_target_cds(
    target: tuple[str, int], health_path: str = "/health", version: int | None = None
) -> str:
    """`cluster_target` 한 개만 담은 CDS 파일(JSON). Envoy는 `cds_path`를 감시해 대상 주소를 바꾼다.

    설정이 처음 깔릴 때의 대상이다. 그 뒤로는 릴레이 워커(`EnvoyProxy.set_target`)가 같은 모양으로 교체한다.
    """
    doc = {
        "version_info": str(version if version is not None else int(time.time())),
        "resources": [
            {"@type": CLUSTER_TYPE, **_cluster("cluster_target", target[0], target[1], health_path)}
        ],
    }
    return json.dumps(doc)


def render_bootstrap(
    origin: tuple[str, int],
    target: tuple[str, int],
    health_path: str = "/health",
    rds_path: str = "/etc/envoy/rds/rds.json",
    cds_path: str | None = None,
) -> str:
    http_manager = "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager"
    router = "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"
    doc = {
        "node": {"id": "cmp-relay-1", "cluster": "cmp-relay"},
        "admin": {"address": {"socket_address": {"address": "127.0.0.1", "port_value": 9901}}},
        "static_resources": {
            "listeners": [
                {
                    "name": "ingress",
                    "address": {"socket_address": {"address": "0.0.0.0", "port_value": 8080}},
                    "filter_chains": [
                        {
                            "filters": [
                                {
                                    "name": "envoy.filters.network.http_connection_manager",
                                    "typed_config": {
                                        "@type": http_manager,
                                        "stat_prefix": "ingress",
                                        "rds": {
                                            "route_config_name": "local_route",
                                            "config_source": {
                                                "resource_api_version": "V3",
                                                "path_config_source": {
                                                    "path": rds_path,
                                                    "watched_directory": {
                                                        "path": rds_path.rsplit("/", 1)[0]
                                                    },
                                                },
                                            },
                                        },
                                        "http_filters": [
                                            {
                                                "name": "envoy.filters.http.router",
                                                "typed_config": {"@type": router},
                                            }
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ],
            # cds_path가 있으면 대상 클러스터는 정적으로 두지 않는다(파일로 바꾼다). 원본은 늘 정적이다.
            "clusters": [_cluster("cluster_origin", origin[0], origin[1], health_path)]
            + ([] if cds_path else [_cluster("cluster_target", target[0], target[1], health_path)]),
        },
    }
    if cds_path:
        doc["dynamic_resources"] = _cds_source(cds_path)
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _cds_source(cds_path: str) -> dict:
    return {
        "cds_config": {
            "resource_api_version": "V3",
            "path_config_source": {
                "path": cds_path,
                "watched_directory": {"path": cds_path.rsplit("/", 1)[0]},
            },
        }
    }


def to_dynamic_bootstrap(bootstrap_yaml: str, cds_path: str) -> tuple[str, str]:
    """이미 깔린 정적 bootstrap을 동적 대상으로 바꾼다. (새 bootstrap, 처음 CDS 파일) 을 돌려준다.

    `cluster_target`을 정적 설정에서 빼서 CDS 파일로 옮기고 나머지는 그대로 둔다(원본 클러스터·가중치·관리 포트).
    지금 대상 설정이 그대로 첫 CDS 값이 되므로, 바꾸는 순간에 트래픽이 가는 곳은 달라지지 않는다.
    """
    doc = yaml.safe_load(bootstrap_yaml)
    if "dynamic_resources" in doc:
        raise ValueError("already dynamic")
    clusters = doc["static_resources"]["clusters"]
    target = next((c for c in clusters if c.get("name") == "cluster_target"), None)
    if target is None:
        raise ValueError("no cluster_target in the bootstrap")
    doc["static_resources"]["clusters"] = [c for c in clusters if c is not target]
    doc["dynamic_resources"] = _cds_source(cds_path)
    cds = {"version_info": str(int(time.time())), "resources": [{"@type": CLUSTER_TYPE, **target}]}
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), json.dumps(cds)


SHADOW_COLLECTOR_PORT = 18090  # cmp_traffic.shadow 복사본 수신 포트(루프백)


def add_shadow_cluster(bootstrap_yaml: str, port: int = SHADOW_COLLECTOR_PORT) -> str:
    """깔린 bootstrap에 그림자 수집기 클러스터(`cluster_shadow`, 127.0.0.1)를 정적으로 더한다(83).

    CDS 파일은 릴레이 워커가 `cluster_target` 하나로 통째로 덮어쓰므로 거기에 두지 않는다. 이미 있으면 그대로.
    헬스 체크는 두지 않는다: 복사본은 응답을 기다리지 않고 버려지며, 수집기가 없으면 복사만 실패한다.
    """
    doc = yaml.safe_load(bootstrap_yaml)
    clusters = doc["static_resources"]["clusters"]
    if any(c.get("name") == "cluster_shadow" for c in clusters):
        return bootstrap_yaml
    shadow = _cluster("cluster_shadow", "127.0.0.1", port, "/")
    shadow.pop("health_checks")
    shadow["type"] = "STATIC"
    shadow["connect_timeout"] = "0.25s"
    clusters.append(shadow)
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def render_wireguard(
    address: str,
    peer_allowed_ips: str,
    endpoint: str | None = None,
    listen_port: int = 51820,
) -> str:
    """중계 서버 쪽 wg0.conf 템플릿. MTU는 실측(1420)으로 고정하고 키는 자리표시자다."""
    for value in (address, peer_allowed_ips, endpoint or "x"):
        if not _LINE_SAFE.fullmatch(value):
            raise ValueError(f"허용하지 않는 값: {value!r}")
    lines = [
        "[Interface]",
        f"Address = {address}",
        f"ListenPort = {listen_port}",
        "MTU = 1420",
        "PrivateKey = <RELAY_PRIVATE_KEY>",
        "",
        "[Peer]",
        "PublicKey = <PEER_PUBLIC_KEY>",
        f"AllowedIPs = {peer_allowed_ips}",
        "PersistentKeepalive = 25",
    ]
    if endpoint:
        lines.append(f"Endpoint = {endpoint}")
    return "\n".join(lines) + "\n"
