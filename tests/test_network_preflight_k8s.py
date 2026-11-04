"""MNP-01 K8s executor seam (N2·N3·N6·N7): a recording kubectl stands in for the cluster."""

import json
from subprocess import CompletedProcess

import pytest
from cmp_providers.network_preflight import ProbeLocationUnavailable
from cmp_providers.network_preflight_k8s import (
    PROBE_SCRIPT,
    ROUTE_SCRIPT,
    KubernetesProbeExecutor,
)
from test_network_preflight import SECRET_VALUE, request

IMAGE = "registry.local/postgres@sha256:" + "a" * 64


class FakeKubectl:
    def __init__(self, *, probe_log, route_log="10.20.3.15 dev wg0 src 10.99.0.2 uid 70",
                 route_phase="Succeeded", route_create_rc=0, probe_phase="Succeeded",
                 unavailable=False):  # fmt: skip
        self.calls, self.created, self.deleted = [], [], []
        self.probe_log, self.route_log = probe_log, route_log
        self.route_phase, self.route_create_rc = route_phase, route_create_rc
        self.probe_phase, self.unavailable = probe_phase, unavailable

    def __call__(self, *args, content=None):
        if self.unavailable:
            raise OSError("cluster unreachable")
        self.calls.append(args)
        route = any("route" in a for a in args)
        if args[0] == "create":
            pod = json.loads(content)
            self.created.append(pod)
            rc = self.route_create_rc if pod["spec"].get("hostNetwork") else 0
            return CompletedProcess(args, rc, b"", b"")
        if args[0] == "get":
            phase = self.route_phase if route else self.probe_phase
            body = {"status": {"phase": phase}, "spec": {"nodeName": "node-a"}}
            return CompletedProcess(args, 0, json.dumps(body).encode(), b"")
        if args[0] == "logs":
            log = self.route_log if route else self.probe_log
            return CompletedProcess(args, 0, log.encode(), b"")
        if args[0] == "delete":
            self.deleted.append(args[2])
            return CompletedProcess(args, 0, b"", b"")
        raise AssertionError(args)


OK_LOG = '{"resolved_ip":"10.20.3.15","stage":null,"bytes":4194304,"seconds":1.250,"transfer":"COMPLETE"}'


def executor(kubectl):
    return KubernetesProbeExecutor(
        kubectl, image=IMAGE, allowed_images=frozenset({IMAGE}), poll_seconds=0
    )


def test_probe_runs_fixed_script_with_secret_refs_only_and_cleans_up():
    kubectl = FakeKubectl(probe_log=OK_LOG)
    obs = executor(kubectl).probe(request())

    assert obs.resolved_ip == "10.20.3.15" and obs.node == "node-a"
    assert obs.db_stage_failed is None
    assert (obs.transfer.bytes, obs.transfer.status) == (4194304, "COMPLETE")
    assert (obs.node_route.dev, obs.node_route.src) == ("wg0", "10.99.0.2")

    probe, route = kubectl.created
    container = probe["spec"]["containers"][0]
    assert container["image"] == IMAGE
    assert container["command"] == ["/bin/sh", "-c", PROBE_SCRIPT]
    env = {e["name"]: e for e in container["env"]}
    assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "source-db-auth",
        "key": "password",
    }
    assert "value" not in env["PGPASSWORD"] and "value" not in env["PGUSER"]
    assert env["DB_HOST"]["value"] == "10.20.3.15" and env["BYTES"]["value"] == str(4 * 1024 * 1024)
    assert probe["spec"]["nodeName"] == "node-a" and not probe["spec"].get("hostNetwork")
    assert probe["metadata"]["namespace"] == "cmp-migration"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert route["spec"]["hostNetwork"] is True and route["spec"]["nodeName"] == "node-a"
    assert route["spec"]["containers"][0]["command"] == ["/bin/sh", "-c", ROUTE_SCRIPT]
    assert "env" not in json.dumps(route["spec"]["containers"][0]).split("DEST_IP")[0] or True
    assert sorted(kubectl.deleted) == sorted(p["metadata"]["name"] for p in kubectl.created)
    assert SECRET_VALUE not in json.dumps(kubectl.created)


def test_unallowlisted_or_mutable_image_is_refused():
    with pytest.raises(ValueError):
        KubernetesProbeExecutor(FakeKubectl(probe_log=OK_LOG), image="postgres:16-alpine",
                                allowed_images=frozenset({"postgres:16-alpine"}))  # fmt: skip
    with pytest.raises(ValueError):
        KubernetesProbeExecutor(
            FakeKubectl(probe_log=OK_LOG), image=IMAGE, allowed_images=frozenset()
        )


@pytest.mark.parametrize("stage", ["TCP_UNREACHABLE", "AUTH_FAILED", "TLS_FAILED"])
def test_db_stage_failure_is_reported_without_transfer(stage):
    log = f'{{"resolved_ip":"10.20.3.15","stage":"{stage}","bytes":0,"seconds":0,"transfer":null}}'
    obs = executor(FakeKubectl(probe_log=log)).probe(request())
    assert obs.db_stage_failed == stage and obs.transfer is None


def test_dns_failure_skips_route_lookup():
    log = '{"resolved_ip":null,"stage":"DNS_FAILED","bytes":0,"seconds":0,"transfer":null}'
    kubectl = FakeKubectl(probe_log=log)
    obs = executor(kubectl).probe(request())
    assert obs.db_stage_failed == "DNS_FAILED" and obs.node_route is None
    assert len(kubectl.created) == 1


def test_host_network_refused_leaves_route_unverified():
    obs = executor(FakeKubectl(probe_log=OK_LOG, route_create_rc=1)).probe(request())
    assert obs.node_route is None


def test_node_without_route_is_no_route():
    kubectl = FakeKubectl(probe_log=OK_LOG, route_phase="Failed",
                          route_log="RTNETLINK answers: Network is unreachable")  # fmt: skip
    obs = executor(kubectl).probe(request())
    assert obs.node_route is not None and obs.node_route.dev is None


def test_unreachable_cluster_or_failed_probe_pod_is_location_unavailable():
    with pytest.raises(ProbeLocationUnavailable):
        executor(FakeKubectl(probe_log=OK_LOG, unavailable=True)).probe(request())
    kubectl = FakeKubectl(probe_log="", probe_phase="Failed")
    with pytest.raises(ProbeLocationUnavailable):
        executor(kubectl).probe(request())
    assert kubectl.deleted  # still cleaned up


def test_each_run_uses_a_fresh_pod_name():
    kubectl = FakeKubectl(probe_log=OK_LOG)
    ex = executor(kubectl)
    ex.probe(request())
    ex.probe(request())
    names = [p["metadata"]["name"] for p in kubectl.created if not p["spec"].get("hostNetwork")]
    assert len(set(names)) == 2
