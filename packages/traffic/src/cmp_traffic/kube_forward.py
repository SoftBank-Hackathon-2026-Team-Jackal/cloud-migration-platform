"""중계 서버 K3s API TCP 전달(티켓 74, ADR-007 2안 4단계).

데모 서버 worker → `중계 서버 사설 IP:<리슨 포트>` → `<Agent 터널 IP>:<kube_api_port>`(Agent 전달기 → K3s API).
TLS는 풀지 않는다(TCP 그대로). 사용자 HTTP 경로 Envoy(8080, cluster_origin/target)와 섞지 않으려고
**별도 Envoy**(관리 포트 127.0.0.1:9902)가 이 모듈이 쓰는 LDS·CDS 파일을 감시한다.

규칙
- 리슨 주소: 중계 서버의 사설 IP 하나(0.0.0.0·공인·루프백·터널 대역 금지).
- 리슨 포트: `base_port + (터널 IP − 터널 대역 네트워크 주소)`. 10.200.0.2 → 16402.
  Agent 터널 IP는 키를 바꿔도 유지되므로(티켓 16 T4) 다른 Agent·순서와 무관하게 같은 포트다.
- 대상: `kube_api_port`가 있고 그 포트가 `allowed_ports`에 있으며 폐기되지 않은 Agent만. 나머지는 리스너가 없다.
- 출발지: 중계 서버 터널 IP(대역 첫 주소, 10.200.0.1). Agent는 이 출발지만 받는다(티켓 73 K4).
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .peers import AgentList

# relay.CLUSTER_TYPE와 같은 값. relay는 yaml을 쓰므로 가져오지 않는다(peer-sync는 표준 라이브러리 + pydantic만).
CLUSTER_TYPE = "type.googleapis.com/envoy.config.cluster.v3.Cluster"

DEFAULT_BASE_PORT = 16400
DEFAULT_DIR = "/etc/envoy-kube"
ADMIN_PORT = 9902  # 사용자 경로 Envoy(9901)와 겹치지 않게
LISTENER_TYPE = "type.googleapis.com/envoy.config.listener.v3.Listener"
TCP_PROXY_TYPE = "type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy"


@dataclass(frozen=True)
class KubeForward:
    agent_id: str
    tunnel_ip: str
    kube_api_port: int
    listen_port: int

    @property
    def name(self) -> str:
        return f"kube_api_{self.listen_port}"


def listen_port(tunnel_ip: str, tunnel_cidr: str, base_port: int = DEFAULT_BASE_PORT) -> int:
    network = ipaddress.IPv4Network(tunnel_cidr)
    ip = ipaddress.IPv4Address(tunnel_ip)
    if ip not in network:
        raise ValueError(f"터널 대역 밖 주소입니다: {tunnel_ip}")
    port = base_port + (int(ip) - int(network.network_address))
    if base_port < 1024 or port > 65535:
        raise ValueError(f"리슨 포트가 범위를 벗어납니다: {port}")
    return port


def check_bind_ip(value: str, tunnel_cidr: str) -> str:
    """리슨 주소는 중계 서버의 사설 IPv4 하나뿐이다(0.0.0.0·공인·루프백·터널 대역 금지)."""
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValueError(f"리슨 주소는 IPv4여야 합니다: {value!r}") from exc
    if ip.is_unspecified or ip.is_loopback or not ip.is_private:
        raise ValueError(f"리슨 주소는 사설 IP여야 합니다: {value}")
    if ip in ipaddress.IPv4Network(tunnel_cidr):
        raise ValueError(f"리슨 주소가 터널 대역 안입니다(Agent 쪽에 열린다): {value}")
    return value


def kube_forwards(
    listing: AgentList, tunnel_cidr: str, base_port: int = DEFAULT_BASE_PORT
) -> list[KubeForward]:
    out: list[KubeForward] = []
    for agent in listing.agents:
        port = agent.kube_api_port
        if agent.revoked or port is None or port not in agent.allowed_ports:
            continue
        out.append(
            KubeForward(
                agent_id=agent.agent_id,
                tunnel_ip=agent.tunnel_ip,
                kube_api_port=port,
                listen_port=listen_port(agent.tunnel_ip, tunnel_cidr, base_port),
            )
        )
    return sorted(out, key=lambda f: f.listen_port)


def render_lds(forwards: list[KubeForward], bind_ip: str, version: str) -> dict:
    resources = [
        {
            "@type": LISTENER_TYPE,
            "name": f.name,
            "address": {"socket_address": {"address": bind_ip, "port_value": f.listen_port}},
            "filter_chains": [
                {
                    "filters": [
                        {
                            "name": "envoy.filters.network.tcp_proxy",
                            "typed_config": {
                                "@type": TCP_PROXY_TYPE,
                                "stat_prefix": f.name,
                                "cluster": f.name,
                            },
                        }
                    ]
                }
            ],
        }
        for f in forwards
    ]
    return {"version_info": version, "resources": resources}


def render_cds(forwards: list[KubeForward], tunnel_cidr: str, version: str) -> dict:
    relay_tunnel_ip = str(ipaddress.IPv4Network(tunnel_cidr).network_address + 1)
    resources = [
        {
            "@type": CLUSTER_TYPE,
            "name": f.name,
            "type": "STATIC",
            "connect_timeout": "3s",
            "upstream_bind_config": {
                "source_address": {"address": relay_tunnel_ip, "port_value": 0}
            },
            "load_assignment": {
                "cluster_name": f.name,
                "endpoints": [
                    {
                        "lb_endpoints": [
                            {
                                "endpoint": {
                                    "address": {
                                        "socket_address": {
                                            "address": f.tunnel_ip,
                                            "port_value": f.kube_api_port,
                                        }
                                    }
                                }
                            }
                        ]
                    }
                ],
            },
        }
        for f in forwards
    ]
    return {"version_info": version, "resources": resources}


def _path_source(path: str) -> dict:
    return {
        "resource_api_version": "V3",
        "path_config_source": {
            "path": path,
            "watched_directory": {"path": path.rsplit("/", 1)[0]},
        },
    }


def render_kube_bootstrap(
    lds_path: str = f"{DEFAULT_DIR}/lds.json",
    cds_path: str = f"{DEFAULT_DIR}/cds.json",
    admin_port: int = ADMIN_PORT,
) -> str:
    """전달 전용 Envoy bootstrap. 정적 리소스 없이 LDS·CDS 파일만 본다. JSON은 YAML이기도 하다."""
    doc = {
        "node": {"id": "cmp-relay-kube-api", "cluster": "cmp-relay"},
        "admin": {
            "address": {"socket_address": {"address": "127.0.0.1", "port_value": admin_port}}
        },
        "dynamic_resources": {
            "lds_config": _path_source(lds_path),
            "cds_config": _path_source(cds_path),
        },
    }
    return json.dumps(doc, indent=2)


def _write_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)  # Envoy는 디렉터리 감시로 move를 본다


class KubeForwardFiles:
    """peer 동기화 뒤에 호출된다(`PeerSyncer(forwards=files.apply)`). 잘못된 bind 주소는 만들 때 거부."""

    def __init__(
        self,
        directory: str | Path,
        bind_ip: str,
        tunnel_cidr: str,
        base_port: int = DEFAULT_BASE_PORT,
    ) -> None:
        self.directory = Path(directory)
        self.bind_ip = check_bind_ip(bind_ip, tunnel_cidr)
        self.tunnel_cidr, self.base_port = tunnel_cidr, base_port

    def apply(self, listing: AgentList) -> list[KubeForward]:
        forwards = kube_forwards(listing, self.tunnel_cidr, self.base_port)
        version = listing.list_version
        # 클러스터를 먼저 둔다: 새 리스너가 아직 없는 클러스터를 가리키지 않게.
        _write_atomic(self.directory / "cds.json", render_cds(forwards, self.tunnel_cidr, version))
        _write_atomic(self.directory / "lds.json", render_lds(forwards, self.bind_ip, version))
        return forwards
