"""중계 서버 배포 자산 생성(#63): Envoy bootstrap과 WireGuard 설정 템플릿.

비밀값(개인키 등)은 파일에 쓰지 않고 자리표시자로만 둔다. 실제 키는 배포 때 주입한다.
관리 포트(9901)는 루프백에만 연다(공개하지 않음).
"""

from __future__ import annotations

import re

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


def render_bootstrap(
    origin: tuple[str, int],
    target: tuple[str, int],
    health_path: str = "/health",
    rds_path: str = "/etc/envoy/rds/rds.json",
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
            "clusters": [
                _cluster("cluster_origin", origin[0], origin[1], health_path),
                _cluster("cluster_target", target[0], target[1], health_path),
            ],
        },
    }
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
