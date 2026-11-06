"""중계 서버 WireGuard peer 동기화(#63) 테스트. 플랫폼 조회 API 응답은 가짜 source로 대신한다."""

from __future__ import annotations

import base64
import json
import urllib.error

import pytest
from cmp_traffic.peers import (
    AgentList,
    AgentPeer,
    HttpAgentSource,
    PeerSyncer,
    WgCli,
    config_from_env,
    plan_peer_changes,
    relay_agents_url,
    run_sync_loop,
    wg_commands,
)

CIDR = "10.200.0.0/24"


def key(n: int) -> str:
    return base64.b64encode(bytes([n]) * 32).decode()


def peer(n: int, ip: str | None = None, revoked: bool = False, ports=(8080,)) -> AgentPeer:
    address = ip or f"10.200.0.{n + 1}"
    return AgentPeer(
        agent_id=f"agent-{n}",
        public_key=key(n),
        tunnel_ip=address,
        allowed_ips=[f"{address}/32"],
        allowed_ports=[] if revoked else list(ports),
        revoked=revoked,
    )


def test_new_agents_are_added_and_revoked_or_missing_ones_are_removed() -> None:
    current = {key(1): "10.200.0.2/32", key(2): "10.200.0.3/32"}
    desired = AgentList(list_version="v2", agents=[peer(2), peer(3), peer(1, revoked=True)])
    plan = plan_peer_changes(current, desired, CIDR)
    assert [p.public_key for p in plan.add] == [key(3)]
    assert plan.remove == [key(1)]


def test_a_peer_whose_tunnel_ip_changed_is_updated_not_duplicated() -> None:
    current = {key(1): "10.200.0.2/32"}
    desired = AgentList(list_version="v2", agents=[peer(1, ip="10.200.0.9")])
    plan = plan_peer_changes(current, desired, CIDR)
    assert [p.tunnel_ip for p in plan.add] == ["10.200.0.9"]
    assert plan.remove == []


def test_unchanged_state_means_no_commands() -> None:
    current = {key(1): "10.200.0.2/32"}
    plan = plan_peer_changes(current, AgentList(list_version="v1", agents=[peer(1)]), CIDR)
    assert not plan.add and not plan.remove


@pytest.mark.parametrize(
    "bad_ip",
    ["10.201.0.5", "10.200.0.1", "10.200.0.0", "10.200.0.255", "not-an-ip", "10.200.0.0/24"],
)
def test_tunnel_ip_outside_the_range_or_reserved_is_rejected(bad_ip) -> None:
    with pytest.raises(ValueError):
        plan_peer_changes({}, AgentList(list_version="v", agents=[peer(1, ip=bad_ip)]), CIDR)


def test_duplicate_keys_or_tunnel_ips_are_rejected() -> None:
    with pytest.raises(ValueError, match="중복"):
        plan_peer_changes({}, AgentList(list_version="v", agents=[peer(1), peer(1)]), CIDR)
    two_on_one_ip = [peer(1, ip="10.200.0.5"), peer(2, ip="10.200.0.5")]
    with pytest.raises(ValueError, match="중복"):
        plan_peer_changes({}, AgentList(list_version="v", agents=two_on_one_ip), CIDR)


@pytest.mark.parametrize("bad_key", ["short", "a" * 44, key(1)[:-1] + "!", ""])
def test_malformed_public_key_is_rejected(bad_key) -> None:
    with pytest.raises(ValueError):
        AgentPeer(
            agent_id="a",
            public_key=bad_key,
            tunnel_ip="10.200.0.2",
            allowed_ips=["10.200.0.2/32"],
            allowed_ports=[],
            revoked=False,
        )


def test_an_empty_list_never_wipes_existing_peers_by_accident() -> None:
    current = {key(1): "10.200.0.2/32"}
    with pytest.raises(ValueError, match="비어"):
        plan_peer_changes(current, AgentList(list_version="v", agents=[]), CIDR)
    plan = plan_peer_changes(
        current, AgentList(list_version="v", agents=[]), CIDR, allow_empty=True
    )
    assert plan.remove == [key(1)]


def test_wg_commands_are_argument_lists_without_a_shell_and_keep_keepalive() -> None:
    plan = plan_peer_changes(
        {key(1): "10.200.0.2/32"}, AgentList(list_version="v", agents=[peer(2)]), CIDR
    )
    commands = wg_commands(plan, interface="wg0")
    assert commands == [
        ["wg", "set", "wg0", "peer", key(1), "remove"],
        [
            "wg", "set", "wg0", "peer", key(2),
            "allowed-ips", "10.200.0.3/32", "persistent-keepalive", "25",
        ],
    ]  # fmt: skip


class FakeSource:
    def __init__(self, lists: list[AgentList | None]) -> None:
        self.lists, self.seen = lists, []

    def fetch(self, known_version: str | None) -> AgentList | None:
        self.seen.append(known_version)
        return self.lists.pop(0)


class FakeWg:
    def __init__(self, peers: dict[str, str] | None = None) -> None:
        self.peers, self.ran = dict(peers or {}), []

    def current_peers(self) -> dict[str, str]:
        return dict(self.peers)

    def run(self, argv: list[str]) -> None:
        self.ran.append(argv)
        if "remove" in argv:
            self.peers.pop(argv[4], None)
        else:
            self.peers[argv[4]] = argv[6]


def test_syncer_skips_work_when_the_version_did_not_change_and_sends_the_known_version() -> None:
    source = FakeSource([AgentList(list_version="v1", agents=[peer(1)]), None])
    wg = FakeWg()
    syncer = PeerSyncer(source, wg, CIDR)
    syncer.sync_once()
    assert wg.ran and syncer.version == "v1"
    wg.ran.clear()
    syncer.sync_once()  # source가 None(변경 없음)을 돌려줌
    assert wg.ran == [] and source.seen == [None, "v1"]


def test_syncer_keeps_running_state_when_the_list_is_invalid() -> None:
    bad = AgentList(list_version="v2", agents=[peer(1, ip="10.201.0.9")])
    source = FakeSource([AgentList(list_version="v1", agents=[peer(1)]), bad])
    wg = FakeWg()
    syncer = PeerSyncer(source, wg, CIDR)
    syncer.sync_once()
    before = dict(wg.peers)
    with pytest.raises(ValueError):
        syncer.sync_once()
    assert (
        wg.peers == before and syncer.version == "v1"
    )  # 잘못된 목록은 적용도, 버전 갱신도 안 한다


def test_wgcli_reads_current_peers_and_runs_commands_without_a_shell() -> None:
    calls = []

    class Done:
        stdout = f"{key(1)}\t10.200.0.2/32\n{key(2)}\t(none)\n"

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Done()

    cli = WgCli("wg0", runner=runner)
    assert cli.current_peers() == {key(1): "10.200.0.2/32"}
    cli.run(["wg", "set", "wg0", "peer", key(1), "remove"])
    assert calls[0][0] == ["wg", "show", "wg0", "allowed-ips"]
    assert all(isinstance(argv, list) and "shell" not in kw for argv, kw in calls)


# --- 플랫폼 실제 응답(GET /relays/{relay_id}/agents, docs/contracts/openapi.yaml RelayAgentList) ---

PLATFORM_RESPONSE = {
    "list_version": "a1b2c3d4e5f60718",
    "agents": [
        {
            "agent_id": "agent-1",
            "public_key": key(1),
            "tunnel_ip": "10.200.0.2",
            "allowed_ips": ["10.200.0.2/32"],
            "allowed_ports": [5432, 8080],
            "revoked": False,
        },
        {
            "agent_id": "agent-2",
            "public_key": key(2),
            "tunnel_ip": "10.200.0.3",
            "allowed_ips": ["10.200.0.3/32"],
            "allowed_ports": [],
            "revoked": True,
        },
    ],
}


def test_the_platform_response_parses_and_revoked_agents_are_removed() -> None:
    listing = AgentList.model_validate(PLATFORM_RESPONSE)
    assert listing.list_version == "a1b2c3d4e5f60718"
    current = {key(2): "10.200.0.3/32"}  # 폐기된 Agent가 아직 peer로 남아 있다
    plan = plan_peer_changes(current, listing, CIDR)
    assert [p.agent_id for p in plan.add] == ["agent-1"]
    assert plan.remove == [key(2)]


@pytest.mark.parametrize(
    "allowed_ips",
    [[], ["0.0.0.0/0"], ["10.200.0.9/32"], ["10.200.0.2/32", "10.200.0.9/32"], ["10.200.0.0/24"]],
)
def test_allowed_ips_that_differ_from_the_tunnel_ip_are_rejected(allowed_ips) -> None:
    broken = json.loads(json.dumps(PLATFORM_RESPONSE["agents"][0]))
    broken["allowed_ips"] = allowed_ips
    with pytest.raises(ValueError):
        AgentPeer.model_validate(broken)


def test_relay_agents_url_is_built_from_the_relay_id_and_rejects_path_tricks() -> None:
    assert (
        relay_agents_url("https://platform.example/api/", "relay-1")
        == "https://platform.example/api/relays/relay-1/agents"
    )
    for bad in ["", "../x", "a/b", "relay 1", "relay?x=1"]:
        with pytest.raises(ValueError):
            relay_agents_url("https://platform.example", bad)


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.raw = json.dumps(body).encode()

    def read(self) -> bytes:
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_http_source_sends_the_bearer_credential_and_skips_when_the_list_version_is_unchanged() -> (
    None
):
    requests = []

    def opener(request, timeout):
        requests.append((request.full_url, dict(request.header_items()), timeout))
        return FakeResponse(PLATFORM_RESPONSE)

    source = HttpAgentSource(
        "https://p.example/relays/r1/agents", token="secret-credential", opener=opener
    )
    first = source.fetch(None)
    assert first is not None and first.list_version == "a1b2c3d4e5f60718"
    assert (
        source.fetch("a1b2c3d4e5f60718") is None
    )  # 서버가 304를 주지 않아도 버전이 같으면 변경 없음
    assert source.fetch("older") is not None
    assert requests[0][1]["Authorization"] == "Bearer secret-credential"
    assert requests[0][0] == "https://p.example/relays/r1/agents"


def test_http_source_errors_do_not_leak_the_credential() -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Relay unauthorized", {}, None)

    source = HttpAgentSource(
        "https://p.example/relays/r1/agents", token="secret-credential", opener=opener
    )
    with pytest.raises(RuntimeError) as caught:
        source.fetch(None)
    assert "403" in str(caught.value) and "secret-credential" not in str(caught.value)


def test_sync_loop_keeps_running_after_a_failed_poll_and_leaves_peers_untouched() -> None:
    good = AgentList.model_validate(PLATFORM_RESPONSE)

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, known_version):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("agent list request failed: HTTP 503")
            return good if known_version != good.list_version else None

    wg, logs, sleeps = FakeWg(), [], []
    syncer = PeerSyncer(Flaky(), wg, CIDR)
    run_sync_loop(
        syncer, interval=30, sleep=sleeps.append, log=logs.append,
        should_stop=lambda: len(sleeps) >= 3,
    )  # fmt: skip
    assert sleeps == [30, 30, 30]
    assert any("503" in line for line in logs)  # 실패는 기록하되 루프는 계속
    assert key(1) in wg.peers and key(2) not in wg.peers


def test_config_needs_the_platform_url_relay_id_and_credential_and_has_safe_defaults() -> None:
    with pytest.raises(ValueError, match="RELAY_CREDENTIAL"):
        config_from_env({"PLATFORM_URL": "https://p.example", "RELAY_ID": "r1"})
    config = config_from_env(
        {"PLATFORM_URL": "https://p.example", "RELAY_ID": "r1", "RELAY_CREDENTIAL": "c"}
    )
    assert config["url"] == "https://p.example/relays/r1/agents"
    assert (config["tunnel_cidr"], config["interface"], config["interval"]) == (
        "10.200.0.0/24", "wg0", 30.0,
    )  # fmt: skip
