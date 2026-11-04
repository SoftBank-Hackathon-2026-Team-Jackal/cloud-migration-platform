import collections
import sys
import threading
import time
import urllib.request

sys.path.insert(0, "/lab/src")
import json
from pathlib import Path

from cmp_traffic import Handover, TrafficPolicy, TrafficShifter
from cmp_traffic.envoy import EnvoyObserver, EnvoyProxy, build_route_config

RDS = Path("/etc/envoy/rds/rds.json")
ADMIN = "http://envoy:9901"
counts = collections.Counter()
stop = threading.Event()


def load():
    while not stop.is_set():
        try:
            with urllib.request.urlopen("http://envoy:8080/", timeout=3) as r:
                counts[("ok", r.headers.get("x-tp-env"))] += 1
        except urllib.error.HTTPError as e:
            counts[(f"http{e.code}", e.headers.get("x-tp-env"))] += 1
        except Exception as e:  # noqa: BLE001 - 시험 부하기: 모든 실패를 센다
            counts[("err", type(e).__name__)] += 1
        time.sleep(0.04)


def inject(path):
    urllib.request.urlopen("http://backend-target:8000" + path, timeout=2).read()


class LabProxy(EnvoyProxy):
    def __init__(self, *a, fault_at=None, fault_path="/inject/fail", **k):
        super().__init__(*a, **k)
        self.fault_at = fault_at
        self.fault_path = fault_path

    def apply(self, weights):
        super().apply(weights)
        print("  applied", weights, "version", self.version, flush=True)
        if self.fault_at is not None and weights["target"] == self.fault_at:
            inject(self.fault_path)
            print("  >> fault injected on target", flush=True)


def scenario(name, fault_at, fault_path="/inject/fail"):
    inject("/inject/ok")
    counts.clear()
    print("==", name, flush=True)
    proxy = LabProxy(RDS, ADMIN, fault_at=fault_at, fault_path=fault_path, confirm_timeout=5)
    proxy.version = int(time.time()) % 100000  # 시나리오 간 버전 충돌 방지
    proxy.apply({"origin": 100, "target": 0})
    shifter = TrafficShifter(proxy, EnvoyObserver(ADMIN))
    policy = TrafficPolicy(
        observe_seconds=4, min_requests=20, max_p99_ratio=3.0, max_step_seconds=30
    )
    result = shifter.run(
        policy, Handover(status="READY", ready=True, external_endpoint="backend-target")
    )
    print("  status:", result.status, result.final_weights)
    for e in result.events:
        print("   event:", e.step, e.decision, e.reason)
    time.sleep(2)
    print("  client counts:", dict(counts), flush=True)
    return result


# 시작: 100/0 설정을 먼저 써서 Envoy가 뜰 수 있게 한다
RDS.write_text(json.dumps(build_route_config({"origin": 100, "target": 0}, 1)))
for i in range(60):
    try:
        urllib.request.urlopen("http://envoy:9901/ready", timeout=1).read()
        break
    except Exception:  # noqa: BLE001 - Envoy 준비 대기
        time.sleep(1)
threading.Thread(target=load, daemon=True).start()
time.sleep(3)
a = scenario("healthy rollout", None)
b = scenario("fault at 50%", 50)
c = scenario("slow target at 50% (p99 gate)", 50, "/inject/delay/1.0")
stop.set()
ok = (
    a.status == "DONE"
    and b.status == "ROLLED_BACK"
    and b.final_weights == {"origin": 100, "target": 0}
    and c.status == "ROLLED_BACK"
    and "p99" in c.events[-1].reason
)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
