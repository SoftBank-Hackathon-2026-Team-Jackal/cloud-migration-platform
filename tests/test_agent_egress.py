"""온프레 Agent의 출발지 공인 IP를 데이터 터널 허용 목록에 자동으로 더한다(auto_target_egress).

테스트 경계: 순수 함수(`client_ip`, `EgressStore`, `resolve_target_egress`)와 터널 입력 모델.
Agent 요청에서 IP를 읽는 HTTP 경로는 `test_tunnel_exchange.py`에 있다.
"""

import pytest
from cmp_api.agent_egress import MAX_CIDRS, TTL_SECONDS, EgressStore, client_ip
from cmp_api.start_request import StartRefused, resolve_target_egress
from cmp_data import tunnel as wg
from pydantic import ValidationError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- 출발지 IP 읽기 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("223.38.86.12", "223.38.86.12"),
        ("211.118.245.138", "211.118.245.138"),
        # 에지(Caddy)가 마지막에 붙인 값을 쓴다: 앞의 값은 클라이언트가 보낸 것일 수 있다.
        ("1.2.3.4, 223.38.86.12", "223.38.86.12"),
    ],
)
def test_the_last_forwarded_address_is_the_public_ip(header, expected) -> None:
    assert client_ip(header) == expected


@pytest.mark.parametrize(
    "header",
    [None, "", "garbage", "10.0.0.5", "172.18.0.4", "192.168.0.7", "127.0.0.1", "169.254.1.1", "::1",
     "2001:db8::1", "223.38.86.12, 10.0.0.5"],
)  # fmt: skip
def test_private_loopback_ipv6_and_garbage_are_not_a_nat_ip(header) -> None:
    assert client_ip(header) is None


def test_a_value_expires_so_a_changed_wifi_is_not_trusted() -> None:
    clock = Clock()
    store = EgressStore(clock)
    store.put("agent-001", "223.38.86.12")
    assert store.get("agent-001") == "223.38.86.12" and store.age("agent-001") == 0
    clock.now += TTL_SECONDS
    assert store.get("agent-001") == "223.38.86.12"
    clock.now += 1
    assert store.get("agent-001") is None
    assert store.get("other-agent") is None


# --- 터널 입력 모델 ---------------------------------------------------------------------------------


def test_auto_tunnel_needs_no_fixed_cidr_but_only_for_the_source_ingress() -> None:
    assert wg.WireGuardTunnel(ingress="source", auto_target_egress=True).egress_cidrs == []
    with pytest.raises(ValidationError):  # 클라우드 대상(ingress=target)은 원본 NAT를 쓴다
        wg.WireGuardTunnel(
            ingress="target", auto_target_egress=True, source_egress_cidrs=["3.36.170.168/32"]
        )
    with pytest.raises(ValidationError):  # 자동도 고정 CIDR도 없으면 예전처럼 거절
        wg.WireGuardTunnel(ingress="source")


def test_an_endpoint_never_opens_without_an_allowed_ip() -> None:
    """Service의 loadBalancerSourceRanges가 비면 쿠버네티스는 모든 주소를 허용한다. 빈 허용 목록은 거절한다."""
    with pytest.raises(ValueError, match="EGRESS_CIDRS_REQUIRED"):
        wg.endpoint_manifest("migration-1", [], ingress="source")
    with pytest.raises(ValueError, match="EGRESS_CIDRS_REQUIRED"):
        wg.standing_endpoint_manifest([])


# --- 시작할 때 채우기 ---------------------------------------------------------------------------------

BASE = {
    "request": {},
    "tunnel": {"ingress": "source", "target_egress_cidrs": ["211.118.245.138/32"], "auto_target_egress": True},
}  # fmt: skip


def seen(ip: str | None, agent_id: str = "agent-001") -> EgressStore:
    store = EgressStore(Clock())
    if ip:
        store.put(agent_id, ip)
    return store


def test_the_agents_current_ip_is_added_and_the_configured_one_stays() -> None:
    out = resolve_target_egress(BASE, "agent-001", seen("223.38.86.12"))
    tunnel = out["tunnel"]
    assert tunnel["target_egress_cidrs"] == ["211.118.245.138/32", "223.38.86.12/32"]
    assert tunnel["auto_target_egress"] is False  # 워커에는 고정된 목록만 간다
    assert BASE["tunnel"]["auto_target_egress"] is True  # 입력을 바꾸지 않는다
    wg.WireGuardTunnel.model_validate(tunnel)


def test_the_same_ip_is_not_added_twice_and_the_list_stays_within_the_limit() -> None:
    same = resolve_target_egress(BASE, "agent-001", seen("211.118.245.138"))
    assert same["tunnel"]["target_egress_cidrs"] == ["211.118.245.138/32"]
    many = {
        "tunnel": {
            **BASE["tunnel"],
            "target_egress_cidrs": [f"10.{i}.0.0/16" for i in range(MAX_CIDRS)],
        }
    }
    out = resolve_target_egress(many, "agent-001", seen("223.38.86.12"))
    assert len(out["tunnel"]["target_egress_cidrs"]) == MAX_CIDRS
    assert out["tunnel"]["target_egress_cidrs"][-1] == "223.38.86.12/32"  # 자동 값은 남는다


def test_without_an_observed_ip_the_configured_cidrs_are_used_and_nothing_is_refused() -> None:
    out = resolve_target_egress(BASE, "agent-001", seen(None))
    assert out["tunnel"]["target_egress_cidrs"] == ["211.118.245.138/32"]
    assert out["tunnel"]["auto_target_egress"] is False
    out = resolve_target_egress(BASE, None, seen("223.38.86.12"))  # 대상 Agent가 정해지지 않았다
    assert out["tunnel"]["target_egress_cidrs"] == ["211.118.245.138/32"]


def test_with_neither_the_start_is_refused_instead_of_opening_the_endpoint() -> None:
    bare = {"tunnel": {"ingress": "source", "auto_target_egress": True}}
    with pytest.raises(StartRefused) as refused:
        resolve_target_egress(bare, "agent-001", seen(None))
    assert refused.value.code == "TARGET_EGRESS_UNKNOWN" and refused.value.status == 409


def test_another_agents_ip_is_never_used() -> None:
    out = resolve_target_egress(BASE, "agent-001", seen("223.38.86.12", agent_id="agent-002"))
    assert out["tunnel"]["target_egress_cidrs"] == ["211.118.245.138/32"]


def test_without_the_flag_or_a_tunnel_the_input_is_untouched() -> None:
    plain = {"tunnel": {"ingress": "source", "target_egress_cidrs": ["211.118.245.138/32"]}}
    assert resolve_target_egress(plain, "agent-001", seen("223.38.86.12")) is plain
    assert resolve_target_egress({"request": {}}, "agent-001", seen("223.38.86.12")) == {
        "request": {}
    }
    assert resolve_target_egress(None, "agent-001", seen("223.38.86.12")) is None
