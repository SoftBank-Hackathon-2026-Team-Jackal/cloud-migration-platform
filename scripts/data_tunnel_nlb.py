"""Pre-create, check or delete the standing source NLB of the data tunnel (Provisioning 82, ADR-007 2안).

With `tunnel.standing_ingress: true` a run reuses `cmp-data-tunnel/cmp-wg-standing-endpoint` on the source EKS
instead of creating an NLB (2-3 minutes). Create it before the demo:

  aws eks update-kubeconfig --name cmp-demo-source --region ap-northeast-2 \\
      --profile cmp-aws-prod --alias cmp-demo-source
  uv run python scripts/data_tunnel_nlb.py up --context cmp-demo-source --cidr <Mac public IP>/32
  uv run python scripts/data_tunnel_nlb.py status --context cmp-demo-source
  uv run python scripts/data_tunnel_nlb.py down --context cmp-demo-source    # after the demo
  uv run python scripts/data_tunnel_nlb.py manifest --cidr <Mac public IP>/32 # print only

`up` waits until the Service has an address, the NLB is `active` (AWS CLI) and its DNS name resolves.
`up`/`down` refuse while a run's gateway is selected (a migration is using it) unless `--force`.
It changes only that one Service. It prints names, addresses and states, never a secret.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

from cmp_data import tunnel as wg

FIELD_MANAGER = "cmp-data-migration"  # same as the worker, so a run can change selector/ranges
Run = Callable[[Sequence[str], bytes | None], str]


def run(args: Sequence[str], stdin: bytes | None = None) -> str:
    return subprocess.run(list(args), input=stdin, capture_output=True, check=True).stdout.decode()


def resolves(host: str) -> bool:
    try:
        return bool(socket.getaddrinfo(host, wg.WG_PORT, proto=socket.IPPROTO_UDP))
    except OSError:
        return False


class Nlb:
    def __init__(self, ns: argparse.Namespace, *, run: Run = run,
                 resolves: Callable[[str], bool] = resolves,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):  # fmt: skip
        self.ns, self._run, self._resolves, self._sleep, self._clock = (
            ns,
            run,
            resolves,
            sleep,
            clock,
        )

    def kubectl(self, *args: str, stdin: bytes | None = None) -> str:
        context = ["--context", self.ns.context] if self.ns.context else []
        return self._run(["kubectl", *context, *args], stdin)

    def service(self) -> dict | None:
        out = self.kubectl("get", "service", wg.STANDING_ENDPOINT, "-n", wg.NAMESPACE,
                           "--ignore-not-found", "-o", "json")  # fmt: skip
        return json.loads(out) if out.strip() else None

    def lb_state(self, host: str) -> str:
        query = f"LoadBalancers[?DNSName=='{host}'].State.Code"
        out = self._run(["aws", "elbv2", "describe-load-balancers", "--profile", self.ns.profile,
                         "--region", self.ns.region, "--query", query, "--output", "text"], None)  # fmt: skip
        return out.strip() or "missing"

    def _wait(self, what: str, check: Callable[[], object]) -> object:
        deadline = self._clock() + self.ns.timeout
        while not (value := check()):
            if self._clock() > deadline:
                raise SystemExit(f"시간 초과: {what}")
            self._sleep(5)
        return value


def _host(item: dict | None) -> str | None:
    ingress = ((item or {}).get("status", {}).get("loadBalancer", {}).get("ingress")) or []
    return (ingress[0].get("hostname") or ingress[0].get("ip")) if ingress else None


def _in_use(item: dict | None) -> str | None:
    selected = ((item or {}).get("spec", {}).get("selector") or {}).get("cmp.dev/data-tunnel")
    return selected if selected and selected != wg.STANDING_IDLE else None


def manifest(cidrs: list[str]) -> list[dict]:
    tunnel = wg.WireGuardTunnel(ingress="source", target_egress_cidrs=cidrs, standing_ingress=True)
    return [wg.namespace_manifest(), wg.standing_endpoint_manifest(tunnel.egress_cidrs)]


def up(nlb: Nlb) -> int:
    items = manifest(nlb.ns.cidr)
    if (busy := _in_use(nlb.service())) and not nlb.ns.force:
        print(f"사용 중: {busy} 실행의 게이트웨이를 고르고 있습니다(--force로 덮어쓰기).")
        return 3
    body = json.dumps({"apiVersion": "v1", "kind": "List", "items": items}).encode()
    nlb.kubectl("apply", "--server-side", "--field-manager", FIELD_MANAGER, "-f", "-", stdin=body)
    host = nlb._wait("Service 주소", lambda: _host(nlb.service()))
    print(f"주소: {host}")
    nlb._wait("NLB active", lambda: nlb.lb_state(host) == "active")
    print("NLB: active")
    nlb._wait("DNS 이름 풀림", lambda: nlb._resolves(host))
    print(
        f"준비됨: {host}:{wg.WG_PORT}/udp 허용 {', '.join(items[1]['spec']['loadBalancerSourceRanges'])}"
    )
    return 0


def status(nlb: Nlb) -> int:
    item = nlb.service()
    if not item:
        print("없음")
        return 1
    host = _host(item)
    print(f"주소: {host or '(대기 중)'}")
    print(f"허용: {', '.join(item['spec'].get('loadBalancerSourceRanges') or [])}")
    print(f"고른 게이트웨이: {_in_use(item) or '없음(대기)'}")
    if host:
        print(f"NLB: {nlb.lb_state(host)}, DNS: {'풀림' if nlb._resolves(host) else '안 풀림'}")
    return 0


def down(nlb: Nlb) -> int:
    if (busy := _in_use(nlb.service())) and not nlb.ns.force:
        print(f"사용 중: {busy} 실행의 게이트웨이를 고르고 있습니다(--force로 지우기).")
        return 3
    # Service finalizer가 NLB를 지울 때까지 기다린다
    nlb.kubectl("delete", "service", wg.STANDING_ENDPOINT, "-n", wg.NAMESPACE,
                "--ignore-not-found", "--wait=true", "--timeout=300s")  # fmt: skip
    print("지움")
    return 0


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["up", "status", "down", "manifest"])
    parser.add_argument(
        "--cidr", action="append", default=[], help="맥(대상) 공인 IP /32, 여러 번 가능"
    )
    parser.add_argument("--context", help="원본 EKS kubectl context")
    parser.add_argument("--profile", default="cmp-aws-prod")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--force", action="store_true")
    ns = parser.parse_args(argv)
    if ns.command in ("up", "manifest") and not ns.cidr:
        parser.error("--cidr가 필요합니다")
    return ns


def main(argv: list[str], **deps) -> int:
    ns = parse(argv)
    if ns.command == "manifest":
        print(json.dumps(manifest(ns.cidr), indent=2))
        return 0
    return {"up": up, "status": status, "down": down}[ns.command](Nlb(ns, **deps))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
