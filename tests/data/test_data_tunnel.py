"""WireGuard 복제 경로(ticket 10): 매니페스트, 인계값 → 양쪽 peer_host, 터널 Activity, Workflow 순서."""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from cmp_data import DataMigrationRequest
from cmp_data import tunnel as wg
from cmp_data.handoff import DataMigrationInput, endpoints
from cmp_worker.activities import data_tunnel as dt
from cmp_worker.workflows.data_migration import DataMigrationWorkflow
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from .test_data_workflow import ARGS, Fakes, wait_phase

MID = "migration-1"
TUNNEL = {"source_egress_cidrs": ["43.200.165.233/32"]}
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
    env = {
        e["name"]: e["value"] for e in items[1]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
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

    def apply(self, manifests):
        self.applied += manifests
        self.log.append((self.name, "apply", [m["kind"] for m in manifests]))

    def secret_data(self, namespace, name):
        return self.secrets.get((namespace, name))

    def put_secret(self, namespace, name, data, *, replace):
        self.secrets[(namespace, name)] = dict(data)
        self.log.append((self.name, "secret", name))

    def lb_address(self, namespace, service):
        self.lb_after -= 1
        return "34.64.1.2" if self.lb_after < 0 else None

    def rollout_wait(self, namespace, deployment, timeout=300):
        self.log.append((self.name, "rollout", deployment))

    def latest_handshake(self, namespace, selector):
        self.handshake_after -= 1
        return 1730900000 if self.handshake_after < 0 else 0


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
           for e in source_env["spec"]["template"]["spec"]["containers"][0]["env"]}  # fmt: skip
    assert env["PEER_ENDPOINT"] == "34.64.1.2:51820"
    # 상대 공개키 = 상대 Secret의 비밀키에서 구한 값. 비밀키는 매니페스트·결과에 없다.
    target_private = made["target"].secrets[(wg.NAMESPACE, wg.name(MID))]["private"]
    assert env["PEER_PUBLIC_KEY"] != target_private
    assert len(base64.b64decode(env["PEER_PUBLIC_KEY"])) == 32
    dumped = json.dumps([made["source"].applied, made["target"].applied, result])
    assert target_private not in dumped
    assert made["source"].secrets[(wg.NAMESPACE, wg.name(MID))]["private"] not in dumped


def test_establish_again_reuses_the_keys(clusters) -> None:
    made, log = clusters
    tunnel = wg.WireGuardTunnel.model_validate(TUNNEL)
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


async def test_workflow_builds_the_tunnel_before_the_precheck() -> None:
    fakes = Fakes()

    @activity.defn(name="dm_tunnel")
    async def tunnel(payload: dict) -> dict:
        fakes.calls.append("dm_tunnel")
        assert payload["tunnel"] == TUNNEL
        return {"name": "cmp-wg-x", "endpoint": "34.64.1.2", "handshake": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        workers = [
            Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[DataMigrationWorkflow]),
            Worker(env.client, task_queue=MIGRATION_QUEUE,
                   activities=[*fakes.activities(), tunnel]),
        ]  # fmt: skip
        async with workers[0], workers[1]:
            handle = await env.client.start_workflow(
                DataMigrationWorkflow.run, {**ARGS, "tunnel": TUNNEL},
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
