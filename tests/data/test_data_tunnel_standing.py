"""Provisioning 82: 원본 NLB 재사용(`standing_ingress`)과 스레드 heartbeat."""

from __future__ import annotations

import threading

import pytest
from cmp_data import tunnel as wg
from cmp_worker.activities import data_tunnel as dt
from pydantic import ValidationError
from temporalio.testing import ActivityEnvironment

from .test_data_tunnel import MID, REVERSE_TUNNEL, TUNNEL, clusters, request  # noqa: F401

STANDING = {**REVERSE_TUNNEL, "standing_ingress": True}


def _lb(cluster) -> list[dict]:
    return [m for m in cluster.applied
            if m["kind"] == "Service" and m["spec"].get("type") == "LoadBalancer"]  # fmt: skip


def test_standing_ingress_is_off_by_default_and_only_for_the_source_ingress() -> None:
    assert wg.WireGuardTunnel.model_validate(REVERSE_TUNNEL).standing_ingress is False
    assert wg.WireGuardTunnel.model_validate(STANDING).standing_ingress is True
    with pytest.raises(ValidationError):
        wg.WireGuardTunnel.model_validate({**TUNNEL, "standing_ingress": True})


def test_standing_endpoint_has_a_fixed_name_and_selects_this_migrations_gateway() -> None:
    lb = wg.endpoint_manifest(MID, ["203.0.113.7/32"], ingress="source", standing=True)
    other = wg.endpoint_manifest("migration-2", ["198.51.100.9/32"], ingress="source",
                                 standing=True)  # fmt: skip
    assert lb["metadata"]["name"] == other["metadata"]["name"] == wg.STANDING_ENDPOINT
    assert lb["metadata"]["namespace"] == wg.NAMESPACE
    # 실행별 라벨이 없다(그 실행의 자원으로 보이지 않게). selector만 이번 실행의 게이트웨이를 고른다.
    assert "cmp.dev/data-tunnel" not in lb["metadata"]["labels"]
    assert lb["metadata"]["labels"]["cmp.dev/data-tunnel-standing"] == "true"
    assert lb["spec"]["selector"] == {"cmp.dev/data-tunnel": wg.name(MID)}
    assert lb["spec"]["loadBalancerSourceRanges"] == ["203.0.113.7/32"]
    per_run = wg.endpoint_manifest(MID, ["203.0.113.7/32"], ingress="source")
    assert lb["metadata"]["annotations"] == per_run["metadata"]["annotations"]
    assert lb["spec"]["loadBalancerClass"] == per_run["spec"]["loadBalancerClass"]


def test_idle_standing_endpoint_selects_no_gateway() -> None:
    lb = wg.standing_endpoint_manifest(["203.0.113.7/32"])
    assert lb["metadata"]["name"] == wg.STANDING_ENDPOINT
    assert lb["spec"]["selector"] == {"cmp.dev/data-tunnel": wg.STANDING_IDLE}
    per_run = wg.endpoint_manifest(MID, ["203.0.113.7/32"], ingress="source", standing=True)
    per_run["spec"]["selector"] = {"cmp.dev/data-tunnel": wg.STANDING_IDLE}
    assert lb == per_run


def test_source_nlb_health_check_is_shortened() -> None:
    notes = wg.endpoint_manifest(MID, ["203.0.113.7/32"], ingress="source")["metadata"][
        "annotations"
    ]
    prefix = "service.beta.kubernetes.io/aws-load-balancer-"
    assert notes[f"{prefix}healthcheck-interval"] == "5"
    assert notes[f"{prefix}healthcheck-timeout"] == "4"
    assert notes[f"{prefix}healthcheck-healthy-threshold"] == "2"
    assert notes[f"{prefix}healthcheck-unhealthy-threshold"] == "2"


def test_establish_reuses_the_standing_endpoint(clusters) -> None:  # noqa: F811
    made, log = clusters
    made["source"].address = "nlb.example.com"
    result = dt.establish(request(), wg.WireGuardTunnel.model_validate(STANDING))
    assert result["endpoint"] == "nlb.example.com"
    (lb,) = _lb(made["source"])
    assert lb["metadata"]["name"] == wg.STANDING_ENDPOINT
    assert lb["spec"]["selector"] == {"cmp.dev/data-tunnel": wg.name(MID)}
    assert lb["spec"]["loadBalancerSourceRanges"] == ["203.0.113.7/32"]
    assert {e[2] for e in log if e[1] == "lb"} == {wg.STANDING_ENDPOINT}
    assert _lb(made["target"]) == []


def test_cleanup_keeps_the_standing_endpoint_and_removes_the_rest(clusters, monkeypatch) -> None:  # noqa: F811
    made, log = clusters
    for side, cluster in made.items():
        monkeypatch.setattr(
            cluster, "delete",
            lambda ns, kind, name, side=side: log.append((side, "delete", (ns, kind, name))),
            raising=False,
        )  # fmt: skip
    tunnel = wg.WireGuardTunnel.model_validate(STANDING)
    log.clear()
    assert dt.cleanup(request(), tunnel) == {"name": wg.name(MID), "cleaned": True}
    deleted = [e[2] for e in log if e[1] == "delete"]
    assert all(name != wg.STANDING_ENDPOINT and not name.endswith("-endpoint")
               for _, _, name in deleted)  # fmt: skip
    base = wg.name(MID)
    for side in ("source", "target"):
        for kind, name in [("deployment", base), ("configmap", base), ("secret", base),
                           ("service", f"{base}-peer-db")]:  # fmt: skip
            assert (side, "delete", (wg.NAMESPACE, kind, name)) in log
    assert ("target", "delete", ("cmp-app", "networkpolicy", f"{base}-db-in")) in log
    assert len(deleted) == 9


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
