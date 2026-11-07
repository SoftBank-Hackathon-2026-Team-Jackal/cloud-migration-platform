"""WireGuard 복제 경로(ticket 10): 매니페스트, 인계값 → 양쪽 peer_host, 터널 Activity, Workflow 순서."""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from cmp_data import DataMigrationRequest
from cmp_data import tunnel as wg
from cmp_data.handoff import DataMigrationInput, endpoints
from cmp_worker.activities import data_tunnel as dt
from cmp_worker.events import configure_emitter
from cmp_worker.workflows.data_migration import DataMigrationWorkflow
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from .test_data_workflow import ARGS, Fakes, wait_phase

MID = "migration-1"
TUNNEL = {"source_egress_cidrs": ["43.200.165.233/32"]}
REVERSE_TUNNEL = {"ingress": "source", "target_egress_cidrs": ["203.0.113.7/32"]}
SOURCE = {"access": "AWS_EKS", "cluster_name": "cmp-demo-source", "region": "ap-northeast-2",
          "namespace": "chat-source", "pod": "chat-db-0",
          "local_host": "chat-db.chat-source.svc.cluster.local", "dbname": "chat"}  # fmt: skip
GIVEN = {"source": SOURCE, "app": {}, "settings": {}, "tunnel": TUNNEL}
GKE_VIEW = {
    "proceed": True,
    "common": {"provider": "GCP", "namespace": "cmp-app",
               "cluster": {"kind": "GKE", "name": "cmp-demo", "location": "asia-northeast3",
                           "account_id": "sbh26-prod"}},
    "database": {"release": "chat-db", "name": "chat", "ready": True},
}  # fmt: skip


# ---- 매니페스트(순수) ----


def test_names_are_per_migration_and_peer_host_is_the_same_on_both_sides() -> None:
    assert wg.name(MID) != wg.name("migration-2")
    assert wg.peer_host(MID) == f"{wg.name(MID)}-peer-db.cmp-data-tunnel.svc.cluster.local"


def test_gateway_manifests_carry_only_the_public_key() -> None:
    items = wg.gateway_manifests(MID, "source", image="alpine:3.22", local_db="db.ns.svc",
                                 peer_public_key="PUB", peer_endpoint="34.1.2.3:51820")  # fmt: skip
    assert [i["kind"] for i in items] == ["ConfigMap", "Deployment", "Service"]
    container = items[1]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
    pod_ip = {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}
    assert pod_ip in container["env"]
    # 대상 DB 송신 정책(TCP 5432만)을 지나도록 Service가 Pod 5432로 넘긴다(클라우드 검증에서 발견)
    assert items[2]["spec"]["ports"][0]["targetPort"] == 5432
    assert env == {"WG_IP": "10.210.0.1", "PEER_IP": "10.210.0.2", "PEER_PUBLIC_KEY": "PUB",
                   "PEER_ENDPOINT": "34.1.2.3:51820", "LOCAL_DB": "db.ns.svc"}  # fmt: skip
    volumes = items[1]["spec"]["template"]["spec"]["volumes"]
    assert {"name": "key", "secret": {"secretName": wg.name(MID)}} in volumes
    assert items[2]["metadata"]["name"] == f"{wg.name(MID)}-peer-db"
    assert all(i["metadata"]["namespace"] == wg.NAMESPACE for i in items)


def test_endpoint_accepts_only_the_source_egress_and_db_policy_only_the_gateway() -> None:
    lb = wg.endpoint_manifest(MID, ["43.200.165.233/32"])
    assert lb["spec"]["type"] == "LoadBalancer"
    assert lb["spec"]["loadBalancerSourceRanges"] == ["43.200.165.233/32"]
    assert lb["spec"]["ports"][0]["protocol"] == "UDP"
    policy = wg.db_ingress_policy(MID, "cmp-app", "chat-db")
    assert policy["metadata"]["namespace"] == "cmp-app"
    assert policy["spec"]["podSelector"] == {
        "matchLabels": {"app.kubernetes.io/instance": "chat-db"}
    }
    peer = policy["spec"]["ingress"][0]["from"][0]
    assert peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": wg.NAMESPACE}
    assert policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 5432}]


@pytest.mark.parametrize("cidrs", [[], ["0.0.0.0/0"], ["10.0.0.0/8"], ["1.2.3.4/24"], ["::1/128"]])
def test_egress_cidrs_must_be_narrow_ipv4(cidrs) -> None:
    with pytest.raises(ValidationError):
        wg.WireGuardTunnel(source_egress_cidrs=cidrs)


# ---- 인계값 → 양쪽 DbEndpoint ----


def test_tunnel_sets_both_peer_hosts_to_the_gateway_service() -> None:
    given = DataMigrationInput.model_validate(GIVEN)  # 원본 peer_host 없이도 된다
    source, target, code = endpoints(GKE_VIEW, given, MID)
    assert code is None
    assert source.peer_host == target.peer_host == wg.peer_host(MID)
    assert source.peer_port == target.peer_port == 5432
    assert source.local_host == SOURCE["local_host"]  # 원본 게이트웨이·cmp-cutover가 쓰는 주소
    assert target.access == "GCP_GKE" and target.pod == "chat-db-0"
    assert target.local_host == "chat-db.cmp-app.svc.cluster.local"


def test_tunnel_needs_the_source_db_service_address() -> None:
    no_local = {**SOURCE}
    no_local.pop("local_host")
    with pytest.raises(ValidationError):
        DataMigrationInput.model_validate({**GIVEN, "source": no_local})


def test_without_tunnel_endpoints_keep_the_given_peer_hosts() -> None:
    plain = {**GIVEN, "tunnel": None, "source": {**SOURCE, "peer_host": "10.0.0.1"}}
    given = DataMigrationInput.model_validate({**plain, "target_peer_host": "10.0.0.2"})
    source, target, code = endpoints(GKE_VIEW, given, MID)
    assert (source.peer_host, target.peer_host, target.local_host, code) == (
        "10.0.0.1", "10.0.0.2", None, None)  # fmt: skip
    blocked = {**GKE_VIEW, "proceed": False, "blocked_by": ["DATABASE_NOT_READY"]}
    assert endpoints(blocked, given, MID) == (None, None, "DATABASE_NOT_READY")


# ---- Activity(가짜 클러스터) ----


class FakeCluster:
    def __init__(self, name: str, log: list, *, lb_after: int = 0, handshake_after: int = 0):
        self.name, self.log, self.secrets, self.applied = name, log, {}, []
        self.lb_after, self.handshake_after = lb_after, handshake_after
        self.address = "34.64.1.2"

    def apply(self, manifests):
        self.applied += manifests
        self.log.append((self.name, "apply", [m["kind"] for m in manifests]))

    def secret_data(self, namespace, name):
        return self.secrets.get((namespace, name))

    def put_secret(self, namespace, name, data, *, replace):
        self.secrets[(namespace, name)] = dict(data)
        self.log.append((self.name, "secret", name))

    def lb_address(self, namespace, service):
        self.log.append((self.name, "lb", service))
        self.lb_after -= 1
        return self.address if self.lb_after < 0 else None

    def rollout_wait(self, namespace, deployment, timeout=300):
        self.log.append((self.name, "rollout", deployment))

    def latest_handshake(self, namespace, selector):
        self.log.append((self.name, "handshake", selector))
        self.handshake_after -= 1
        return 1730900000 if self.handshake_after < 0 else 0

    # Provisioning 83: 원본 고정 게이트웨이
    peer_line = None
    run = None

    def set_standing_peer(self, line, *, only_if_run=None):
        self.log.append((self.name, "peer", line, only_if_run))
        current = (self.peer_line or "").split(" ")[0]
        if only_if_run is None or current == only_if_run:
            self.peer_line = line

    def standing_run(self):
        return self.run

    def delete(self, namespace, kind, name):
        self.log.append((self.name, "delete", (namespace, kind, name)))


@pytest.fixture
def clusters(monkeypatch):
    log: list = []
    made = {"source": FakeCluster("source", log), "target": FakeCluster("target", log, lb_after=2)}

    from contextlib import contextmanager

    @contextmanager
    def open_cluster(endpoint):
        yield made["source" if endpoint.access == "AWS_EKS" else "target"]

    monkeypatch.setattr(dt, "OPEN_CLUSTER", open_cluster)
    monkeypatch.setattr(dt, "SLEEP", lambda s: None)
    return made, log


def request() -> DataMigrationRequest:
    source, target, _ = endpoints(GKE_VIEW, DataMigrationInput.model_validate(GIVEN), MID)
    return DataMigrationRequest(migration_id=MID, source=source, target=target)


def test_establish_waits_for_the_target_lb_then_points_the_source_at_it(clusters) -> None:
    made, log = clusters
    result = dt.establish(request(), wg.WireGuardTunnel.model_validate(TUNNEL))
    assert result == {"name": wg.name(MID), "endpoint": "34.64.1.2", "handshake": True,
                      "peer_host": wg.peer_host(MID)}  # fmt: skip
    applies = [(side, kinds) for side, verb, kinds in log if verb == "apply"]
    assert applies[2] == (
        "target",
        ["ConfigMap", "Deployment", "Service", "Service", "NetworkPolicy"],
    )
    assert applies[3] == ("source", ["ConfigMap", "Deployment", "Service"])
    source_env = next(m for m in made["source"].applied if m["kind"] == "Deployment")
    env = {e["name"]: e["value"]
           for e in source_env["spec"]["template"]["spec"]["containers"][0]["env"]
           if "value" in e}  # fmt: skip
    assert env["PEER_ENDPOINT"] == "34.64.1.2:51820"
    # 상대 공개키 = 상대 Secret의 비밀키에서 구한 값. 비밀키는 매니페스트·결과에 없다.
    target_private = made["target"].secrets[(wg.NAMESPACE, wg.name(MID))]["private"]
    assert env["PEER_PUBLIC_KEY"] != target_private
    assert len(base64.b64decode(env["PEER_PUBLIC_KEY"])) == 32
    dumped = json.dumps([made["source"].applied, made["target"].applied, result])
    assert target_private not in dumped
    assert made["source"].secrets[(wg.NAMESPACE, wg.name(MID))]["private"] not in dumped


async def test_tunnel_activity_tells_the_web_it_is_building_the_tunnel(clusters) -> None:
    # Provisioning 77: the source NLB takes 2-3 minutes; the web shows `data.phase` "tunnel"
    # meanwhile, like dm_precheck emits "preflight" when it starts.
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    try:
        payload = {"request": request().model_dump(mode="json"), "tunnel": TUNNEL}
        await ActivityEnvironment().run(dt.dm_tunnel, payload)
    finally:
        configure_emitter(previous)
    assert [(e.type, e.phase, e.data) for e in emitter.events] == [
        ("data.phase", "data_copy", {"phase": "tunnel"})
    ]


@pytest.mark.parametrize("config", [TUNNEL, REVERSE_TUNNEL])
def test_establish_again_reuses_the_keys(clusters, config) -> None:
    made, log = clusters
    tunnel = wg.WireGuardTunnel.model_validate(config)
    dt.establish(request(), tunnel)
    keys = {k: dict(v) for k, v in made["target"].secrets.items()}
    made["target"].lb_after = 0
    dt.establish(request(), tunnel)
    assert made["target"].secrets == keys
    assert [e for e in log if e[1] == "secret"] == [("target", "secret", wg.name(MID)),
                                                     ("source", "secret", wg.name(MID))]  # fmt: skip


def test_no_lb_address_is_a_retryable_timeout(clusters, monkeypatch) -> None:
    made, _ = clusters
    made["target"].lb_after = 10**9
    ticks = iter(range(0, 10**6, 100))
    monkeypatch.setattr(dt.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ApplicationError) as exc:
        dt.establish(request(), wg.WireGuardTunnel.model_validate(TUNNEL))
    assert exc.value.type == "TUNNEL_ENDPOINT_TIMEOUT" and not exc.value.non_retryable


# ---- Workflow: 터널이 사전 점검보다 먼저 ----


@pytest.mark.parametrize("config,timeout_s", [(TUNNEL, 1800), (REVERSE_TUNNEL, 3600)])
async def test_workflow_builds_the_tunnel_before_the_precheck(config, timeout_s) -> None:
    fakes = Fakes()

    @activity.defn(name="dm_tunnel")
    async def tunnel(payload: dict) -> dict:
        fakes.calls.append("dm_tunnel")
        assert payload["tunnel"] == config
        assert activity.info().start_to_close_timeout.total_seconds() == timeout_s
        return {"name": "cmp-wg-x", "endpoint": "34.64.1.2", "handshake": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        workers = [
            Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[DataMigrationWorkflow]),
            Worker(env.client, task_queue=MIGRATION_QUEUE,
                   activities=[*fakes.activities(), tunnel]),
        ]  # fmt: skip
        async with workers[0], workers[1]:
            handle = await env.client.start_workflow(
                DataMigrationWorkflow.run, {**ARGS, "tunnel": config},
                id=f"data-{uuid.uuid4()}", task_queue=WORKFLOW_QUEUE,
            )  # fmt: skip
            await wait_phase(handle, "streaming")
            status = await handle.query(DataMigrationWorkflow.status)
            await handle.signal(DataMigrationWorkflow.finish)
            await handle.result()
    assert fakes.calls[:2] == ["dm_tunnel", "dm_precheck"]
    assert status["tunnel"]["endpoint"] == "34.64.1.2"


async def test_workflow_without_tunnel_input_does_not_call_it() -> None:
    fakes = Fakes()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        workers = [
            Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[DataMigrationWorkflow]),
            Worker(env.client, task_queue=MIGRATION_QUEUE, activities=fakes.activities()),
        ]
        async with workers[0], workers[1]:
            handle = await env.client.start_workflow(
                DataMigrationWorkflow.run, ARGS, id=f"data-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )  # fmt: skip
            await wait_phase(handle, "streaming")
            await handle.signal(DataMigrationWorkflow.finish)
            await handle.result()
    assert fakes.calls[0] == "dm_precheck"


# ---- 입구 방향 계약(Provisioning 67, #297) ----


def test_ingress_contract_defaults_and_round_trips() -> None:
    legacy = wg.WireGuardTunnel.model_validate(TUNNEL)
    assert legacy.ingress == "target"
    reverse = wg.WireGuardTunnel.model_validate(REVERSE_TUNNEL)
    assert reverse.source_egress_cidrs is None
    assert reverse.target_egress_cidrs == ["203.0.113.7/32"]
    assert wg.WireGuardTunnel.model_validate(reverse.model_dump()) == reverse
    assert wg.WireGuardTunnel.model_validate(legacy.model_dump()) == legacy


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"ingress": "source"},
        {"ingress": "unknown", **TUNNEL},
        {"ingress": "source", **TUNNEL},
        {"target_egress_cidrs": ["203.0.113.7/32"]},
        {**REVERSE_TUNNEL, **TUNNEL},
        {**TUNNEL, "target_egress_cidrs": ["203.0.113.7/32"]},
    ],
)
def test_ingress_rejects_missing_or_mixed_egress_cidrs(value) -> None:
    with pytest.raises(ValidationError):
        wg.WireGuardTunnel.model_validate(value)


@pytest.mark.parametrize(
    "cidrs",
    [
        [],
        ["0.0.0.0/1"],
        ["10.0.0.0/15"],
        ["1.2.3.4/24"],
        ["::1/128"],
        ["not-a-cidr"],
        ["203.0.113.7/32"] * 9,
    ],
)
def test_target_egress_cidrs_must_be_narrow_ipv4(cidrs) -> None:
    with pytest.raises(ValidationError):
        wg.WireGuardTunnel.model_validate({**REVERSE_TUNNEL, "target_egress_cidrs": cidrs})


def test_target_egress_accepts_any_ipv4_alone_for_onprem_ingress() -> None:
    """온프레 NAT IP는 와이파이마다 바뀐다: 원본 NLB 입구만 0.0.0.0/0 하나로 열 수 있다(인증은 WireGuard 키)."""
    tunnel = wg.WireGuardTunnel.model_validate(
        {**REVERSE_TUNNEL, "target_egress_cidrs": ["0.0.0.0/0"]}
    )
    assert tunnel.egress_cidrs == ["0.0.0.0/0"]
    lb = wg.endpoint_manifest(MID, tunnel.egress_cidrs, ingress="source", standing=True)
    assert lb["spec"]["loadBalancerSourceRanges"] == ["0.0.0.0/0"]


def test_any_ipv4_absorbs_other_target_cidrs() -> None:
    """auto_target_egress가 /32를 더해도 0.0.0.0/0이 있으면 그것 하나로 합친다."""
    value = {**REVERSE_TUNNEL, "target_egress_cidrs": ["0.0.0.0/0", "203.0.113.7/32"]}
    assert wg.WireGuardTunnel.model_validate(value).egress_cidrs == ["0.0.0.0/0"]


def test_source_egress_still_rejects_any_ipv4() -> None:
    """원본 NAT 쪽(ingress=target)은 고정 IP라 넓게 열 이유가 없다."""
    with pytest.raises(ValidationError):
        wg.WireGuardTunnel(source_egress_cidrs=["0.0.0.0/0"])


@pytest.mark.parametrize(
    "ingress,field", [("target", "source_egress_cidrs"), ("source", "target_egress_cidrs")]
)
def test_egress_accepts_the_16_boundary(ingress, field) -> None:
    assert wg.WireGuardTunnel.model_validate({"ingress": ingress, field: ["203.0.0.0/16"]})


def test_source_endpoint_uses_public_lbc_nlb_with_cross_zone_and_tcp_health() -> None:
    lb = wg.endpoint_manifest(MID, ["203.0.113.7/32"], ingress="source")
    assert lb["spec"]["loadBalancerClass"] == "service.k8s.aws/nlb"
    assert lb["spec"]["loadBalancerSourceRanges"] == ["203.0.113.7/32"]
    assert lb["spec"]["ports"] == [
        {"name": "wg", "port": 51820, "targetPort": 51820, "protocol": "UDP"}
    ]
    prefix = "service.beta.kubernetes.io/aws-load-balancer-"
    assert lb["metadata"]["annotations"] == {
        f"{prefix}scheme": "internet-facing",
        f"{prefix}nlb-target-type": "ip",
        f"{prefix}attributes": "load_balancing.cross_zone.enabled=true",
        f"{prefix}healthcheck-protocol": "tcp",
        f"{prefix}healthcheck-port": "8080",
        f"{prefix}healthcheck-interval": "5",
        f"{prefix}healthcheck-timeout": "4",
        f"{prefix}healthcheck-healthy-threshold": "2",
        f"{prefix}healthcheck-unhealthy-threshold": "2",
    }
    legacy = wg.endpoint_manifest(MID, TUNNEL["source_egress_cidrs"])
    assert "loadBalancerClass" not in legacy["spec"]
    assert "annotations" not in legacy["metadata"]


@pytest.mark.parametrize("side", ["source", "target"])
def test_reverse_gateways_keep_db_routes_and_add_health_only_at_the_ingress(side) -> None:
    items = wg.gateway_manifests(
        MID,
        side,
        image="alpine:3.22",
        local_db="db.ns.svc",
        peer_public_key="PUBLIC",
        peer_endpoint="nlb.example.com:51820" if side == "target" else None,
        ingress="source",
    )
    container = items[1]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
    assert env["KEEPALIVE"] == "25"
    assert env["PEER_ENDPOINT"] == ("nlb.example.com:51820" if side == "target" else "")
    assert (env.get("HEALTH_PORT") == "8080") == (side == "source")
    assert ({"name": "health", "containerPort": 8080} in container["ports"]) == (side == "source")
    script = items[0]["data"]["start.sh"]
    assert 'persistent-keepalive "${KEEPALIVE:-15}"' in script
    assert 'TCP-LISTEN:"$HEALTH_PORT"' in script
    assert 'TCP-LISTEN:5432,bind="$WG_IP"' in script
    assert 'TCP-LISTEN:5432,bind="$POD_IP"' in script
    assert container["readinessProbe"]["exec"]["command"] == ["test", "-f", "/tmp/started"]
    assert items[2]["metadata"]["name"] == f"{wg.name(MID)}-peer-db"
    assert items[2]["spec"]["ports"][0]["targetPort"] == 5432


@pytest.mark.parametrize(
    "ingress,caller,config",
    [
        ("target", "source", TUNNEL),
        ("source", "target", REVERSE_TUNNEL),
    ],
)
def test_establish_uses_only_the_selected_ingress_and_connects_the_other_side(
    clusters,
    ingress,
    caller,
    config,
) -> None:
    made, log = clusters
    made[ingress].address = "nlb.example.com"
    made[ingress].lb_after = 2
    made[caller].handshake_after = 2
    result = dt.establish(request(), wg.WireGuardTunnel.model_validate(config))
    assert result == {
        "name": wg.name(MID),
        "endpoint": "nlb.example.com",
        "handshake": True,
        "peer_host": wg.peer_host(MID),
    }
    for side in ("source", "target"):
        lbs = [
            m
            for m in made[side].applied
            if m["kind"] == "Service" and m["spec"].get("type") == "LoadBalancer"
        ]
        assert len(lbs) == (1 if side == ingress else 0)
        if lbs:
            assert lbs[0]["spec"]["loadBalancerSourceRanges"] == config[f"{caller}_egress_cidrs"]
        deployment = next(m for m in made[side].applied if m["kind"] == "Deployment")
        env = {
            e["name"]: e["value"]
            for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
            if "value" in e
        }
        assert env["PEER_ENDPOINT"] == ("nlb.example.com:51820" if side == caller else "")
        policies = [m for m in made[side].applied if m["kind"] == "NetworkPolicy"]
        assert policies == (
            [wg.db_ingress_policy(MID, "cmp-app", "chat-db")] if side == "target" else []
        )
    assert {side for side, verb, _ in log if verb == "lb"} == {ingress}
    assert {side for side, verb, _ in log if verb == "handshake"} == {caller}
    lb_index = next(i for i, (_, verb, _) in enumerate(log) if verb == "lb")
    ingress_apply = next(
        i
        for i, (side, verb, kinds) in enumerate(log)
        if side == ingress and verb == "apply" and "Deployment" in kinds
    )
    caller_apply = next(
        i
        for i, (side, verb, kinds) in enumerate(log)
        if side == caller and verb == "apply" and "Deployment" in kinds
    )
    assert ingress_apply < lb_index < caller_apply
    dumped = json.dumps([made["source"].applied, made["target"].applied, result, log])
    # 실제 생성한 비밀키를 assertion diff에 넣지 않는다.
    assert all(
        data["private"] not in dumped
        for cluster in made.values()
        for data in cluster.secrets.values()
    )


def test_reverse_establish_allows_nlb_health_to_lag_the_dns_address(clusters, monkeypatch) -> None:
    made, _ = clusters
    clock = [0.0]
    made["source"].address = "nlb.example.com"
    monkeypatch.setattr(dt.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dt, "SLEEP", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    # DNS is immediate. Model 210 seconds until UDP reaches the healthy NLB target.
    monkeypatch.setattr(made["target"], "latest_handshake", lambda *_: int(clock[0] >= 210))
    assert dt.establish(request(), wg.WireGuardTunnel.model_validate(REVERSE_TUNNEL))["handshake"]
    assert 210 <= clock[0] < 600


@pytest.mark.parametrize("ingress,config", [("source", REVERSE_TUNNEL), ("target", TUNNEL)])
@pytest.mark.parametrize("stage", ["endpoint", "handshake"])
def test_establish_timeouts_remain_retryable_for_both_directions(
    clusters,
    monkeypatch,
    ingress,
    config,
    stage,
) -> None:
    made, _ = clusters
    caller = "target" if ingress == "source" else "source"
    setattr(
        made[ingress if stage == "endpoint" else caller],
        "lb_after" if stage == "endpoint" else "handshake_after",
        10**9,
    )
    ticks = iter(range(0, 10**6, 10))
    monkeypatch.setattr(dt.time, "monotonic", lambda: next(ticks))
    tunnel = wg.WireGuardTunnel.model_validate(
        {**config, "endpoint_timeout_s": 30, "handshake_timeout_s": 10}
    )
    with pytest.raises(ApplicationError) as exc:
        dt.establish(request(), tunnel)
    assert exc.value.type == f"TUNNEL_{stage.upper()}_TIMEOUT"
    assert not exc.value.non_retryable


def test_reverse_onprem_handoff_keeps_peer_hosts_and_kubeconfig_access() -> None:
    view = {
        **GKE_VIEW,
        "common": {"provider": "ON_PREM", "namespace": "cmp-app", "cluster": {"kind": "K3S"}},
    }
    given = DataMigrationInput.model_validate(
        {**GIVEN, "tunnel": REVERSE_TUNNEL, "target_kubeconfig_ref": "k3d-demo"}
    )
    source, target, code = endpoints(view, given, MID)
    assert code is None
    assert source.peer_host == target.peer_host == wg.peer_host(MID)
    assert target.access == "KUBECONFIG" and target.kubeconfig_ref == "k3d-demo"
    assert target.local_host == "chat-db.cmp-app.svc.cluster.local"


@pytest.mark.parametrize("ingress,config", [("target", TUNNEL), ("source", REVERSE_TUNNEL)])
def test_cleanup_deletes_ingress_service_before_gateways_and_preserves_shared_resources(
    clusters,
    monkeypatch,
    ingress,
    config,
) -> None:
    made, log = clusters
    for side, cluster in made.items():
        monkeypatch.setattr(
            cluster,
            "delete",
            lambda ns, kind, name, side=side: log.append((side, "delete", (ns, kind, name))),
            raising=False,
        )
    tunnel = wg.WireGuardTunnel.model_validate(config)
    dt.establish(request(), tunnel)
    log.clear()
    assert dt.cleanup(request(), tunnel) == {"name": wg.name(MID), "cleaned": True}
    expected_lb = (ingress, "delete", (wg.NAMESPACE, "service", f"{wg.name(MID)}-endpoint"))
    assert log[0] == expected_lb
    for side in ("source", "target"):
        for kind, name in [
            ("deployment", wg.name(MID)),
            ("configmap", wg.name(MID)),
            ("secret", wg.name(MID)),
            ("service", f"{wg.name(MID)}-peer-db"),
        ]:
            assert (side, "delete", (wg.NAMESPACE, kind, name)) in log
    assert ("target", "delete", ("cmp-app", "networkpolicy", f"{wg.name(MID)}-db-in")) in log
    assert len(log) == 10  # no Namespace, unrelated migration, DB, or source DB policy deletion
    first = list(log)
    log.clear()
    dt.cleanup(request(), tunnel)
    assert log == first  # missing resources are ignored by the cluster adapter


def test_cleanup_stops_on_lb_deletion_failure_and_can_be_retried(clusters, monkeypatch) -> None:
    made, log = clusters
    fail = [True]

    def delete(ns, kind, name):
        log.append(("source", "delete", (ns, kind, name)))
        if fail[0]:
            raise dt.CommandError("kubectl")

    monkeypatch.setattr(made["source"], "delete", delete, raising=False)
    monkeypatch.setattr(
        made["target"],
        "delete",
        lambda *args: log.append(("target", "delete", args)),
        raising=False,
    )
    tunnel = wg.WireGuardTunnel.model_validate(REVERSE_TUNNEL)
    with pytest.raises(dt.CommandError):
        dt.cleanup(request(), tunnel)
    assert log == [("source", "delete", (wg.NAMESPACE, "service", f"{wg.name(MID)}-endpoint"))]
    fail[0] = False
    assert dt.cleanup(request(), tunnel)["cleaned"]


def test_cluster_delete_waits_for_finalizers_and_ignores_missing_resources() -> None:
    class Session:
        def __init__(self):
            self.calls = []

        def _kubectl(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return b""

    session = Session()
    dt.TunnelCluster(session).delete(wg.NAMESPACE, "service", f"{wg.name(MID)}-endpoint")
    args, kwargs = session.calls[0]
    assert args == (
        "delete",
        "service",
        f"{wg.name(MID)}-endpoint",
        "-n",
        wg.NAMESPACE,
        "--ignore-not-found",
        "--wait=true",
        "--timeout=300s",
    )
    assert kwargs == {"timeout": 330}


async def test_cleanup_activity_exposes_only_result_or_safe_retryable_error(
    clusters, monkeypatch
) -> None:
    payload = {"request": request().model_dump(), "tunnel": REVERSE_TUNNEL}
    made, _ = clusters
    for cluster in made.values():
        monkeypatch.setattr(cluster, "delete", lambda *args: None, raising=False)
    assert await dt.dm_tunnel_cleanup(payload) == {"name": wg.name(MID), "cleaned": True}

    def fail(*args):
        raise dt.CommandError("kubectl")

    monkeypatch.setattr(made["source"], "delete", fail)
    with pytest.raises(ApplicationError) as exc:
        await dt.dm_tunnel_cleanup(payload)
    assert exc.value.type == "TUNNEL_CLEANUP_FAILED" and not exc.value.non_retryable
