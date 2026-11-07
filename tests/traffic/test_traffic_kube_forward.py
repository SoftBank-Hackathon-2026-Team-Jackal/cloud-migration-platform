"""중계 서버 K3s API TCP 전달(티켓 74) 테스트. 플랫폼 Agent 목록 → 전용 Envoy LDS·CDS 파일."""

from __future__ import annotations

import base64
import json

import pytest
import yaml
from cmp_traffic.kube_forward import (
    DEFAULT_BASE_PORT,
    KubeForwardFiles,
    check_bind_ip,
    kube_forwards,
    listen_port,
    render_cds,
    render_kube_bootstrap,
    render_lds,
)
from cmp_traffic.peers import AgentList, AgentPeer, PeerSyncer, config_from_env

CIDR = "10.200.0.0/24"
BIND = "172.31.2.84"


def key(n: int) -> str:
    return base64.b64encode(bytes([n]) * 32).decode()


def peer(n: int, kube: int | None = 6443, revoked: bool = False) -> AgentPeer:
    ip = f"10.200.0.{n + 1}"
    ports = [] if revoked else sorted({5432, 8080} | ({kube} if kube else set()))
    return AgentPeer(
        agent_id=f"agent-{n}",
        public_key=key(n),
        tunnel_ip=ip,
        allowed_ips=[f"{ip}/32"],
        allowed_ports=ports,
        kube_api_port=None if revoked else kube,
        revoked=revoked,
    )


def listing(*agents: AgentPeer, version: str = "v1") -> AgentList:
    return AgentList(list_version=version, agents=list(agents))


# --- 포트 결정 규칙 -----------------------------------------------------------------------------


def test_listen_port_is_the_base_plus_the_tunnel_host_number() -> None:
    assert DEFAULT_BASE_PORT == 16400
    assert listen_port("10.200.0.2", CIDR) == 16402
    assert listen_port("10.200.0.254", CIDR) == 16654
    assert listen_port("10.200.0.7", CIDR, base_port=20000) == 20007


def test_listen_port_does_not_depend_on_other_agents_or_their_order() -> None:
    a, b = peer(1), peer(5)
    first = {f.agent_id: f.listen_port for f in kube_forwards(listing(a, b), CIDR)}
    second = {f.agent_id: f.listen_port for f in kube_forwards(listing(b), CIDR)}
    assert first == {"agent-1": 16402, "agent-5": 16406}
    assert second["agent-5"] == first["agent-5"]


def test_listen_port_outside_the_tunnel_range_or_beyond_65535_is_rejected() -> None:
    with pytest.raises(ValueError):
        listen_port("10.201.0.2", CIDR)
    with pytest.raises(ValueError):
        listen_port("10.200.0.254", CIDR, base_port=65400)


# --- 대상 선정 ----------------------------------------------------------------------------------


def test_revoked_or_unexposed_agents_get_no_forward() -> None:
    agents = listing(peer(1), peer(2, kube=None), peer(3, revoked=True))
    assert [f.agent_id for f in kube_forwards(agents, CIDR)] == ["agent-1"]


def test_a_kube_api_port_the_relay_may_not_reach_is_ignored() -> None:
    # 중계 서버가 실제로 터널로 보낼 수 있는 포트(allowed_ports)에 없으면 전달하지 않는다.
    odd = peer(1).model_copy(update={"allowed_ports": [5432, 8080]})
    assert kube_forwards(listing(odd), CIDR) == []


def test_older_platform_without_the_field_means_no_forward() -> None:
    raw = peer(1).model_dump()
    raw.pop("kube_api_port")
    assert AgentPeer.model_validate(raw).kube_api_port is None


# --- bind 주소 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["0.0.0.0", "3.35.10.10", "127.0.0.1", "10.200.0.1", "10.200.0.9", "::", "not-an-ip", ""],
)
def test_bind_address_must_be_a_private_ip_outside_the_tunnel_range(bad) -> None:
    with pytest.raises(ValueError):
        check_bind_ip(bad, CIDR)


def test_private_relay_address_is_accepted() -> None:
    assert check_bind_ip(BIND, CIDR) == BIND


# --- 생성 설정 ----------------------------------------------------------------------------------


def test_listeners_bind_only_the_private_address_and_proxy_tcp_to_the_agent() -> None:
    forwards = kube_forwards(listing(peer(1), peer(2)), CIDR)
    lds = render_lds(forwards, BIND, version="v1")
    assert lds["version_info"] == "v1"
    listeners = lds["resources"]
    assert [x["name"] for x in listeners] == ["kube_api_16402", "kube_api_16403"]
    first = listeners[0]
    assert first["@type"].endswith("envoy.config.listener.v3.Listener")
    assert first["address"]["socket_address"] == {"address": BIND, "port_value": 16402}
    (chain,) = first["filter_chains"]
    (proxy,) = chain["filters"]
    assert proxy["name"] == "envoy.filters.network.tcp_proxy"
    assert proxy["typed_config"]["cluster"] == "kube_api_16402"
    assert "0.0.0.0" not in json.dumps(lds)


def test_clusters_point_at_the_agent_tunnel_ip_and_kube_api_port_from_the_relay_tunnel_ip() -> None:
    forwards = kube_forwards(listing(peer(1, kube=7443)), CIDR)
    cds = render_cds(forwards, CIDR, version="v1")
    (cluster,) = cds["resources"]
    assert cluster["@type"].endswith("envoy.config.cluster.v3.Cluster")
    assert cluster["name"] == "kube_api_16402"
    assert cluster["type"] == "STATIC"
    endpoint = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]
    assert endpoint["address"]["socket_address"] == {"address": "10.200.0.2", "port_value": 7443}
    # Agent는 출발지 10.200.0.1(중계 서버 터널 IP)만 받는다.
    assert cluster["upstream_bind_config"]["source_address"]["address"] == "10.200.0.1"


def test_no_agents_means_empty_but_valid_resource_files() -> None:
    assert render_lds([], BIND, version="v0")["resources"] == []
    assert render_cds([], CIDR, version="v0")["resources"] == []


def test_bootstrap_is_separate_from_the_user_envoy_and_keeps_admin_on_loopback() -> None:
    doc = yaml.safe_load(
        render_kube_bootstrap("/etc/envoy-kube/lds.json", "/etc/envoy-kube/cds.json")
    )
    assert doc["admin"]["address"]["socket_address"] == {"address": "127.0.0.1", "port_value": 9902}
    assert doc["node"]["id"] != "cmp-relay-1"
    dynamic = doc["dynamic_resources"]
    assert dynamic["lds_config"]["path_config_source"]["path"] == "/etc/envoy-kube/lds.json"
    assert dynamic["cds_config"]["path_config_source"]["path"] == "/etc/envoy-kube/cds.json"
    assert "static_resources" not in doc  # 사용자 경로(8080·cluster_origin/target)와 섞지 않는다


# --- 파일 쓰기와 peer 동기화 연결 ----------------------------------------------------------------


def test_files_are_written_and_a_revoked_agent_listener_is_removed(tmp_path) -> None:
    files = KubeForwardFiles(tmp_path, BIND, CIDR)
    files.apply(listing(peer(1), peer(2), version="v1"))
    lds = json.loads((tmp_path / "lds.json").read_text())
    assert [x["name"] for x in lds["resources"]] == ["kube_api_16402", "kube_api_16403"]

    files.apply(listing(peer(1), peer(2, revoked=True), version="v2"))
    lds = json.loads((tmp_path / "lds.json").read_text())
    cds = json.loads((tmp_path / "cds.json").read_text())
    assert [x["name"] for x in lds["resources"]] == ["kube_api_16402"]
    assert [x["name"] for x in cds["resources"]] == ["kube_api_16402"]
    assert lds["version_info"] == cds["version_info"] == "v2"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cds.json", "lds.json"]  # 임시 파일 없음


def test_bad_bind_address_is_refused_before_anything_is_written(tmp_path) -> None:
    with pytest.raises(ValueError):
        KubeForwardFiles(tmp_path, "0.0.0.0", CIDR)
    assert list(tmp_path.iterdir()) == []


class FakeSource:
    def __init__(self, listing: AgentList) -> None:
        self.listing = listing

    def fetch(self, known_version):
        return None if self.listing.list_version == known_version else self.listing


class FakeWg:
    def __init__(self) -> None:
        self.peers: dict[str, str] = {}

    def current_peers(self) -> dict[str, str]:
        return dict(self.peers)

    def run(self, argv: list[str]) -> None:
        if argv[-1] == "remove":
            self.peers.pop(argv[4], None)
        else:
            self.peers[argv[4]] = argv[6]


def test_peer_sync_writes_forward_files_only_after_peers_are_applied(tmp_path) -> None:
    files = KubeForwardFiles(tmp_path, BIND, CIDR)
    syncer = PeerSyncer(FakeSource(listing(peer(1))), FakeWg(), CIDR, forwards=files.apply)
    syncer.sync_once()
    assert json.loads((tmp_path / "lds.json").read_text())["resources"][0]["name"] == (
        "kube_api_16402"
    )


def test_invalid_list_leaves_existing_forward_files_untouched(tmp_path) -> None:
    files = KubeForwardFiles(tmp_path, BIND, CIDR)
    files.apply(listing(peer(1), version="v1"))
    before = (tmp_path / "lds.json").read_text()
    duplicate = peer(2).model_copy(update={"public_key": key(1)})
    syncer = PeerSyncer(
        FakeSource(listing(peer(1), duplicate, version="v2")), FakeWg(), CIDR, forwards=files.apply
    )
    with pytest.raises(ValueError):
        syncer.sync_once()
    assert (tmp_path / "lds.json").read_text() == before


def test_forwarding_is_off_unless_the_bind_address_is_configured() -> None:
    base = {"PLATFORM_URL": "http://p", "RELAY_ID": "relay-1", "RELAY_CREDENTIAL": "x"}
    assert config_from_env(base)["kube_forward"] is None
    enabled = config_from_env({**base, "KUBE_FORWARD_BIND_IP": BIND})["kube_forward"]
    assert enabled == {"bind_ip": BIND, "directory": "/etc/envoy-kube", "base_port": 16400}
    custom = config_from_env(
        {
            **base,
            "KUBE_FORWARD_BIND_IP": BIND,
            "KUBE_FORWARD_DIR": "/x",
            "KUBE_FORWARD_BASE_PORT": "20000",
        }
    )["kube_forward"]
    assert custom == {"bind_ip": BIND, "directory": "/x", "base_port": 20000}


def test_cluster_type_matches_the_relay_module_without_importing_yaml() -> None:
    from cmp_traffic import kube_forward, relay

    assert kube_forward.CLUSTER_TYPE == relay.CLUSTER_TYPE


def test_enable_script_runs_the_python_that_has_pydantic() -> None:
    # Provisioning 78: on the relay host `python3` is 3.9 without pydantic; peer-sync and
    # relay-worker run /usr/bin/python3.11. The script's import of cmp_traffic must use it too.
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[2] / "infra/relay/enable-kube-api-forward.sh"
    ).read_text()
    assert 'PYTHON="${PYTHON:-python3.11}"' in script
    assert 'PYTHONPATH="$LIB" "$PYTHON" -' in script
    assert 'PYTHONPATH="$LIB" python3 -' not in script
