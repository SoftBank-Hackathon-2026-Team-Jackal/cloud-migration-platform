"""Provisioning 82·83: 원본 NLB(`standing_ingress`)와 원본 게이트웨이 재사용, 스레드 heartbeat.

83 (2026-11-07 `cmp-wg-2aa45ef7`): 원본 게이트웨이 Pod가 08:02:24에 Ready였지만 NLB 대상 등록(08:02:25)
뒤 첫 UDP flow는 08:03에야 생겼다. 실행마다 새 Pod를 만들면 그동안 handshake가 버려진다. 그래서 원본
게이트웨이를 고정 Deployment로 두고 peer만 바꾼다.
"""

from __future__ import annotations

import base64
import threading

import pytest
from cmp_data import tunnel as wg
from cmp_worker.activities import data_tunnel as dt
from pydantic import ValidationError
from temporalio.testing import ActivityEnvironment

from .test_data_tunnel import MID, REVERSE_TUNNEL, SOURCE, TUNNEL, clusters, request  # noqa: F401

STANDING = {**REVERSE_TUNNEL, "standing_ingress": True}


def _lb(cluster) -> list[dict]:
    return [m for m in cluster.applied
            if m["kind"] == "Service" and m["spec"].get("type") == "LoadBalancer"]  # fmt: skip


def _kinds(cluster, kind: str) -> list[dict]:
    return [m for m in cluster.applied if m["kind"] == kind]


def test_standing_ingress_is_off_by_default_and_only_for_the_source_ingress() -> None:
    assert wg.WireGuardTunnel.model_validate(REVERSE_TUNNEL).standing_ingress is False
    assert wg.WireGuardTunnel.model_validate(STANDING).standing_ingress is True
    with pytest.raises(ValidationError):
        wg.WireGuardTunnel.model_validate({**TUNNEL, "standing_ingress": True})


def test_standing_endpoint_always_selects_the_standing_gateway_and_names_the_run() -> None:
    lb = wg.endpoint_manifest(MID, ["203.0.113.7/32"], ingress="source", standing=True)
    other = wg.endpoint_manifest("migration-2", ["198.51.100.9/32"], ingress="source",
                                 standing=True)  # fmt: skip
    assert lb["metadata"]["name"] == other["metadata"]["name"] == wg.STANDING_ENDPOINT
    assert lb["metadata"]["namespace"] == wg.NAMESPACE
    assert "cmp.dev/data-tunnel" not in lb["metadata"]["labels"]
    assert lb["metadata"]["labels"]["cmp.dev/data-tunnel-standing"] == "true"
    # selector가 실행마다 같아야 NLB 대상(게이트웨이 Pod IP)이 바뀌지 않는다
    assert lb["spec"]["selector"] == other["spec"]["selector"] == wg.STANDING_SELECTOR
    assert lb["metadata"]["annotations"][wg.RUN_ANNOTATION] == wg.name(MID)
    assert lb["spec"]["loadBalancerSourceRanges"] == ["203.0.113.7/32"]
    per_run = wg.endpoint_manifest(MID, ["203.0.113.7/32"], ingress="source")
    assert per_run["spec"]["selector"] == {"cmp.dev/data-tunnel": wg.name(MID)}
    lb_notes = {k: v for k, v in lb["metadata"]["annotations"].items() if k != wg.RUN_ANNOTATION}
    assert lb_notes == per_run["metadata"]["annotations"]
    assert lb["spec"]["loadBalancerClass"] == per_run["spec"]["loadBalancerClass"]


def test_idle_standing_endpoint_selects_the_gateway_with_no_run() -> None:
    lb = wg.standing_endpoint_manifest(["203.0.113.7/32"])
    assert lb["metadata"]["name"] == wg.STANDING_ENDPOINT
    assert lb["spec"]["selector"] == wg.STANDING_SELECTOR
    assert lb["metadata"]["annotations"][wg.RUN_ANNOTATION] == wg.STANDING_IDLE


def test_standing_gateway_template_has_no_per_run_values() -> None:
    a, b = wg.standing_gateway_manifests(), wg.standing_gateway_manifests()
    assert a == b  # 다시 apply해도 Pod가 바뀌지 않는다
    config, deployment = a
    assert config["data"]["start.sh"] == wg.STANDING_SCRIPT
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["WG_IP"] == "10.210.0.1" and env["PEER_IP"] == "10.210.0.2"
    assert env["KEEPALIVE"] == "25" and env["HEALTH_PORT"] == "8080"
    assert "PEER_PUBLIC_KEY" not in env and "LOCAL_DB" not in env
    assert deployment["spec"]["template"]["metadata"]["labels"]["cmp.dev/data-tunnel"] == (
        wg.STANDING_GATEWAY
    )
    assert {"name": "key", "secret": {"secretName": wg.STANDING_GATEWAY}} in spec["volumes"]
    assert {"name": "peer", "emptyDir": {}} in spec["volumes"]


def test_standing_peer_line_rejects_whitespace() -> None:
    assert wg.standing_peer_line(MID, "PUB", "db.ns.svc") == f"{wg.name(MID)} PUB db.ns.svc\n"
    for bad in ("", "a b"):
        with pytest.raises(ValueError):
            wg.standing_peer_line(MID, bad, "db.ns.svc")


def test_source_nlb_health_check_is_shortened() -> None:
    notes = wg.endpoint_manifest(MID, ["203.0.113.7/32"], ingress="source")["metadata"][
        "annotations"
    ]
    prefix = "service.beta.kubernetes.io/aws-load-balancer-"
    assert notes[f"{prefix}healthcheck-interval"] == "5"
    assert notes[f"{prefix}healthcheck-timeout"] == "4"
    assert notes[f"{prefix}healthcheck-healthy-threshold"] == "2"
    assert notes[f"{prefix}healthcheck-unhealthy-threshold"] == "2"


def test_establish_reuses_the_standing_gateway_and_sets_its_peer_first(clusters) -> None:  # noqa: F811
    made, log = clusters
    made["source"].address = "nlb.example.com"
    result = dt.establish(request(), wg.WireGuardTunnel.model_validate(STANDING))
    assert result == {"name": wg.name(MID), "endpoint": "nlb.example.com", "handshake": True,
                      "peer_host": wg.peer_host(MID)}  # fmt: skip
    source, target = made["source"], made["target"]
    (lb,) = _lb(source)
    assert lb["metadata"]["name"] == wg.STANDING_ENDPOINT
    assert lb["spec"]["selector"] == wg.STANDING_SELECTOR
    assert lb["spec"]["loadBalancerSourceRanges"] == ["203.0.113.7/32"]
    assert _lb(target) == []
    # 원본: 고정 게이트웨이(실행별 Deployment 없음) + peer-db Service가 고정 게이트웨이를 고른다
    assert [d["metadata"]["name"] for d in _kinds(source, "Deployment")] == [wg.STANDING_GATEWAY]
    (peer_db,) = [
        s for s in _kinds(source, "Service") if s["metadata"]["name"].endswith("-peer-db")
    ]
    assert peer_db["spec"]["selector"] == wg.STANDING_SELECTOR
    assert (wg.NAMESPACE, wg.STANDING_GATEWAY) in source.secrets
    # 원본 peer 설정 = 대상 공개키 + 원본 DB 주소. 대상 게이트웨이보다 먼저.
    run, peer_key, local_db = source.peer_line.split()
    assert run == wg.name(MID) and local_db == SOURCE["local_host"]
    target_env = {e["name"]: e.get("value")
                  for e in _kinds(target, "Deployment")[0]["spec"]["template"]["spec"]
                  ["containers"][0]["env"]}  # fmt: skip
    assert target_env["PEER_ENDPOINT"] == "nlb.example.com:51820"
    assert len(base64.b64decode(peer_key)) == 32
    assert peer_key != target.secrets[(wg.NAMESPACE, wg.name(MID))]["private"]
    order = [(e[0], e[1]) for e in log if e[1] in ("peer", "apply", "handshake")]
    assert order.index(("source", "peer")) < max(
        i for i, e in enumerate(order) if e == ("target", "apply")
    )
    assert ("target", "handshake") in order


def test_cleanup_keeps_the_standing_objects_and_clears_this_runs_peer(clusters) -> None:  # noqa: F811
    made, log = clusters
    tunnel = wg.WireGuardTunnel.model_validate(STANDING)
    dt.establish(request(), tunnel)
    made["source"].run = wg.name(MID)
    log.clear()
    made["source"].applied.clear()
    assert dt.cleanup(request(), tunnel) == {"name": wg.name(MID), "cleaned": True}
    deleted = [(e[0], e[2]) for e in log if e[1] == "delete"]
    standing = {wg.STANDING_ENDPOINT, wg.STANDING_GATEWAY}
    assert not any(name in standing for _, (_, _, name) in deleted)
    base = wg.name(MID)
    assert ("source", (wg.NAMESPACE, "service", f"{base}-peer-db")) in deleted
    for kind, name in [("deployment", base), ("configmap", base), ("secret", base),
                       ("service", f"{base}-peer-db")]:  # fmt: skip
        assert ("target", (wg.NAMESPACE, kind, name)) in deleted
    assert ("target", ("cmp-app", "networkpolicy", f"{base}-db-in")) in deleted
    assert len(deleted) == 6
    assert made["source"].peer_line == ""
    (idle,) = _lb(made["source"])
    assert idle["metadata"]["annotations"][wg.RUN_ANNOTATION] == wg.STANDING_IDLE


def test_cleanup_leaves_another_runs_peer_alone(clusters) -> None:  # noqa: F811
    made, _ = clusters
    made["source"].peer_line = "cmp-wg-other PUB db\n"
    made["source"].run = "cmp-wg-other"
    dt.cleanup(request(), wg.WireGuardTunnel.model_validate(STANDING))
    assert made["source"].peer_line == "cmp-wg-other PUB db\n"
    assert _lb(made["source"]) == []


async def test_tunnel_heartbeats_reach_the_activity_from_the_worker_thread(clusters) -> None:  # noqa: F811
    # temporalio의 async Activity heartbeat는 이벤트 루프에서 task를 만든다. asyncio.to_thread 안에서
    # 그대로 부르면 보내지지 않고 "coroutine ... was never awaited" 경고만 남는다(데모 worker 로그).
    made, _ = clusters
    made["target"].lb_after = 2
    beats: list[tuple[int, tuple]] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append((threading.get_ident(), details))
    payload = {"request": request().model_dump(mode="json"), "tunnel": TUNNEL}
    loop_thread = threading.get_ident()
    await env.run(dt.dm_tunnel, payload)
    assert beats, "no heartbeat reached the activity"
    assert {ident for ident, _ in beats} == {loop_thread}
    assert beats[0][1] == ("waiting for the target UDP load balancer",)
