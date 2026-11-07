"""Provisioning 82: 고정 원본 NLB 미리 만들기/확인/지우기 스크립트(kubectl·aws·DNS는 가짜)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from cmp_data import tunnel as wg

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/data_tunnel_nlb.py"
spec = importlib.util.spec_from_file_location("data_tunnel_nlb", SCRIPT)
nlb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nlb)

HOST = "k8s-cmpdatat-cmpwgsta-1.elb.ap-northeast-2.amazonaws.com"


class Fake:
    def __init__(self, *, existing=None, host_after=1, active_after=1, dns_after=1):
        self.calls, self.applied, self.item = [], None, existing
        self.host_after, self.active_after, self.dns_after = host_after, active_after, dns_after

    def run(self, args, stdin):
        self.calls.append(list(args))
        if args[0] == "aws":
            self.active_after -= 1
            return "active\n" if self.active_after < 0 else "provisioning\n"
        verb = args[3] if args[1] == "--context" else args[1]
        if verb == "apply":
            self.applied = json.loads(stdin)["items"]
            self.item = json.loads(json.dumps(self.applied[1]))
        elif verb == "get":
            if self.item is None:
                return ""
            self.host_after -= 1
            if self.host_after < 0:
                self.item["status"] = {"loadBalancer": {"ingress": [{"hostname": HOST}]}}
            return json.dumps(self.item)
        elif verb == "delete":
            self.item = None
        return ""

    def resolves(self, host):
        self.dns_after -= 1
        return self.dns_after < 0


def main(argv, fake):
    return nlb.main(argv, run=fake.run, resolves=fake.resolves, sleep=lambda s: None)


def test_up_applies_the_idle_standing_service_and_waits_for_active_and_dns(capsys) -> None:
    fake = Fake()
    assert main(["up", "--context", "src", "--cidr", "203.0.113.7/32"], fake) == 0
    assert fake.applied == [wg.namespace_manifest(),
                            wg.standing_endpoint_manifest(["203.0.113.7/32"])]  # fmt: skip
    apply = next(c for c in fake.calls if "apply" in c)
    assert apply[:3] == ["kubectl", "--context", "src"]
    assert ["--server-side", "--field-manager", "cmp-data-migration"] == apply[4:7]
    aws = next(c for c in fake.calls if c[0] == "aws")
    assert aws[aws.index("--profile") + 1] == "cmp-aws-prod"
    assert aws[aws.index("--region") + 1] == "ap-northeast-2"
    assert HOST in aws[aws.index("--query") + 1]
    assert fake.dns_after < 0
    assert f"준비됨: {HOST}:51820/udp" in capsys.readouterr().out


def test_up_refuses_while_a_run_selects_its_gateway_unless_forced() -> None:
    busy = wg.endpoint_manifest("migration-1", ["203.0.113.7/32"], ingress="source", standing=True)
    fake = Fake(existing=busy)
    assert main(["up", "--cidr", "203.0.113.7/32"], fake) == 3
    assert fake.applied is None
    assert main(["up", "--cidr", "198.51.100.9/32", "--force"], fake) == 0
    assert fake.applied[1]["spec"]["loadBalancerSourceRanges"] == ["198.51.100.9/32"]


def test_up_times_out_when_the_nlb_never_becomes_active() -> None:
    fake = Fake(active_after=10**9)
    ticks = iter(range(0, 10**6, 5))
    with pytest.raises(SystemExit, match="NLB active"):
        nlb.main(["up", "--cidr", "203.0.113.7/32", "--timeout", "30"], run=fake.run,
                 resolves=fake.resolves, sleep=lambda s: None, clock=lambda: next(ticks))  # fmt: skip


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "10.0.0.0/8", "::1/128"])
def test_wide_or_ipv6_cidrs_are_rejected(cidr) -> None:
    with pytest.raises(ValueError):
        main(["manifest", "--cidr", cidr], Fake())


def test_down_deletes_only_the_standing_service_and_refuses_while_in_use() -> None:
    busy = wg.endpoint_manifest("migration-1", ["203.0.113.7/32"], ingress="source", standing=True)
    fake = Fake(existing=busy)
    assert main(["down"], fake) == 3
    assert not any("delete" in c for c in fake.calls)
    fake.item = wg.standing_endpoint_manifest(["203.0.113.7/32"])
    assert main(["down"], fake) == 0
    delete = next(c for c in fake.calls if "delete" in c)
    assert delete[1:] == ["delete", "service", wg.STANDING_ENDPOINT, "-n", wg.NAMESPACE,
                          "--ignore-not-found", "--wait=true", "--timeout=300s"]  # fmt: skip


def test_status_reports_missing_and_present(capsys) -> None:
    assert main(["status"], Fake()) == 1
    fake = Fake(existing=wg.standing_endpoint_manifest(["203.0.113.7/32"]), host_after=0,
                active_after=0, dns_after=0)  # fmt: skip
    assert main(["status"], fake) == 0
    out = capsys.readouterr().out
    assert f"주소: {HOST}" in out and "고른 게이트웨이: 없음(대기)" in out and "NLB: active" in out
