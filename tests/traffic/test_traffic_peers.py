"""중계 서버 WireGuard peer 동기화(#63) 테스트. 플랫폼 조회 API 응답은 가짜 source로 대신한다."""

from __future__ import annotations

import base64

import pytest
from cmp_traffic.peers import (
    AgentList,
    AgentPeer,
    PeerSyncer,
    WgCli,
    plan_peer_changes,
    wg_commands,
)

CIDR = "10.200.0.0/24"


def key(n: int) -> str:
    return base64.b64encode(bytes([n]) * 32).decode()


def peer(n: int, ip: str | None = None, status: str = "ACTIVE", ports=(8080,)) -> AgentPeer:
    return AgentPeer(
        agent_id=f"agent-{n}",
        public_key=key(n),
        tunnel_ip=ip or f"10.200.0.{n + 1}",
        allowed_ports=list(ports),
        status=status,
    )


def test_new_agents_are_added_and_revoked_or_missing_ones_are_removed() -> None:
    current = {key(1): "10.200.0.2/32", key(2): "10.200.0.3/32"}
    desired = AgentList(version="v2", agents=[peer(2), peer(3), peer(1, status="REVOKED")])
    plan = plan_peer_changes(current, desired, CIDR)
    assert [p.public_key for p in plan.add] == [key(3)]
    assert plan.remove == [key(1)]


def test_a_peer_whose_tunnel_ip_changed_is_updated_not_duplicated() -> None:
    current = {key(1): "10.200.0.2/32"}
    desired = AgentList(version="v2", agents=[peer(1, ip="10.200.0.9")])
    plan = plan_peer_changes(current, desired, CIDR)
    assert [p.tunnel_ip for p in plan.add] == ["10.200.0.9"]
    assert plan.remove == []


def test_unchanged_state_means_no_commands() -> None:
    current = {key(1): "10.200.0.2/32"}
    plan = plan_peer_changes(current, AgentList(version="v1", agents=[peer(1)]), CIDR)
    assert not plan.add and not plan.remove


@pytest.mark.parametrize(
    "bad_ip",
    ["10.201.0.5", "10.200.0.1", "10.200.0.0", "10.200.0.255", "not-an-ip", "10.200.0.0/24"],
)
def test_tunnel_ip_outside_the_range_or_reserved_is_rejected(bad_ip) -> None:
    with pytest.raises(ValueError):
        plan_peer_changes({}, AgentList(version="v", agents=[peer(1, ip=bad_ip)]), CIDR)


def test_duplicate_keys_or_tunnel_ips_are_rejected() -> None:
    with pytest.raises(ValueError, match="중복"):
        plan_peer_changes({}, AgentList(version="v", agents=[peer(1), peer(1)]), CIDR)
    two_on_one_ip = [peer(1, ip="10.200.0.5"), peer(2, ip="10.200.0.5")]
    with pytest.raises(ValueError, match="중복"):
        plan_peer_changes({}, AgentList(version="v", agents=two_on_one_ip), CIDR)


@pytest.mark.parametrize("bad_key", ["short", "a" * 44, key(1)[:-1] + "!", ""])
def test_malformed_public_key_is_rejected(bad_key) -> None:
    with pytest.raises(ValueError):
        AgentPeer(agent_id="a", public_key=bad_key, tunnel_ip="10.200.0.2")


def test_an_empty_list_never_wipes_existing_peers_by_accident() -> None:
    current = {key(1): "10.200.0.2/32"}
    with pytest.raises(ValueError, match="비어"):
        plan_peer_changes(current, AgentList(version="v", agents=[]), CIDR)
    plan = plan_peer_changes(current, AgentList(version="v", agents=[]), CIDR, allow_empty=True)
    assert plan.remove == [key(1)]


def test_wg_commands_are_argument_lists_without_a_shell_and_keep_keepalive() -> None:
    plan = plan_peer_changes(
        {key(1): "10.200.0.2/32"}, AgentList(version="v", agents=[peer(2)]), CIDR
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
    source = FakeSource([AgentList(version="v1", agents=[peer(1)]), None])
    wg = FakeWg()
    syncer = PeerSyncer(source, wg, CIDR)
    syncer.sync_once()
    assert wg.ran and syncer.version == "v1"
    wg.ran.clear()
    syncer.sync_once()  # source가 None(변경 없음)을 돌려줌
    assert wg.ran == [] and source.seen == [None, "v1"]


def test_syncer_keeps_running_state_when_the_list_is_invalid() -> None:
    bad = AgentList(version="v2", agents=[peer(1, ip="10.201.0.9")])
    source = FakeSource([AgentList(version="v1", agents=[peer(1)]), bad])
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
