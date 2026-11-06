"""infra/data-migration/onprem-poc/poc.py: 2안(원본 EKS 입구, NAT 뒤 k3d가 접속) 일회용 매니페스트(티켓 69)."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "infra/data-migration/onprem-poc/poc.py"
spec = importlib.util.spec_from_file_location("onprem_poc", SCRIPT)
poc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poc)

PUB = "A" * 43 + "="


def _kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def _env(deploy):
    return {
        e["name"]: e.get("value")
        for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def test_every_object_lives_in_poc_namespace_and_has_no_secret():
    for docs in (
        poc.eks_manifests(peer_public_key=PUB, source_cidrs=["203.0.113.7/32"]),
        poc.k3d_manifests(peer_public_key=PUB, endpoint="nlb.example:51820"),
    ):
        assert docs[0] == {"apiVersion": "v1", "kind": "Namespace",
                           "metadata": {"name": "cmp-dm-poc", "labels": poc.LABELS}}  # fmt: skip
        assert all(d["metadata"].get("namespace") == "cmp-dm-poc" for d in docs[1:])
        assert not _kind(docs, "Secret")  # 비밀키는 up.sh가 stdin으로 따로 넣는다
        assert "secretName" in json.dumps(docs)  # 키는 Secret 파일로만 읽는다


def test_eks_entry_is_internet_facing_udp_nlb_open_only_to_given_cidrs():
    docs = poc.eks_manifests(peer_public_key=PUB, source_cidrs=["203.0.113.7/32"])
    [svc] = [s for s in _kind(docs, "Service") if s["spec"].get("type") == "LoadBalancer"]
    ann = svc["metadata"]["annotations"]
    assert svc["spec"]["loadBalancerClass"] == "service.k8s.aws/nlb"  # AWS LB Controller가 맡는다
    assert ann["service.beta.kubernetes.io/aws-load-balancer-scheme"] == "internet-facing"
    assert ann["service.beta.kubernetes.io/aws-load-balancer-nlb-target-type"] == "ip"
    # UDP는 헬스체크가 안 되므로 게이트웨이의 TCP 헬스 포트로 확인한다
    assert ann["service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol"] == "TCP"
    assert ann["service.beta.kubernetes.io/aws-load-balancer-healthcheck-port"] == str(
        poc.HEALTH_PORT
    )
    assert svc["spec"]["loadBalancerSourceRanges"] == ["203.0.113.7/32"]
    assert svc["spec"]["ports"] == [
        {"name": "wg", "port": 51820, "targetPort": 51820, "protocol": "UDP"}
    ]


def test_eks_gateway_listens_and_forwards_tunnel_5432_to_source_db_without_endpoint():
    docs = poc.eks_manifests(peer_public_key=PUB, source_cidrs=["203.0.113.7/32"])
    [deploy] = _kind(docs, "Deployment")
    env = _env(deploy)
    assert env["WG_IP"] == "10.211.0.1" and env["PEER_IP"] == "10.211.0.2"
    assert env["PEER_PUBLIC_KEY"] == PUB
    assert env["PEER_ENDPOINT"] == ""  # 입구 쪽은 기다리기만 한다
    assert env["LOCAL_DB"] == "chat-db-0.chat-db.chat-source.svc.cluster.local"
    ports = deploy["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert {"name": "wg", "containerPort": 51820, "protocol": "UDP"} in ports
    assert {"name": "health", "containerPort": poc.HEALTH_PORT} in ports


def test_k3d_gateway_dials_out_with_keepalive_25_and_exposes_source_db_service():
    docs = poc.k3d_manifests(peer_public_key=PUB, endpoint="nlb.example:51820")
    [deploy] = _kind(docs, "Deployment")
    env = _env(deploy)
    assert env["WG_IP"] == "10.211.0.2" and env["PEER_IP"] == "10.211.0.1"
    assert env["PEER_ENDPOINT"] == "nlb.example:51820"
    assert env["KEEPALIVE"] == "25"
    [svc] = _kind(docs, "Service")
    assert svc["metadata"]["name"] == "src-db"
    assert svc["spec"].get("type") is None  # 클러스터 안에서만
    assert svc["spec"]["ports"] == [{"name": "postgres", "port": 5432, "targetPort": 5432}]


def test_start_script_keeps_key_in_file_and_uses_multiarch_alpine():
    for docs in (
        poc.eks_manifests(peer_public_key=PUB, source_cidrs=["203.0.113.7/32"]),
        poc.k3d_manifests(peer_public_key=PUB, endpoint="nlb.example:51820"),
    ):
        [cm] = _kind(docs, "ConfigMap")
        script = cm["data"]["start.sh"]
        assert "private-key /etc/wg/private" in script
        assert 'persistent-keepalive "$KEEPALIVE"' in script
        [deploy] = _kind(docs, "Deployment")
        assert (
            deploy["spec"]["template"]["spec"]["containers"][0]["image"]
            == "docker.io/library/alpine:3.22"
        )


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "10.0.0.0/8", "203.0.113.7/33", "::/0", "x"])
def test_eks_rejects_wide_or_invalid_source_cidrs(cidr):
    with pytest.raises(ValueError):
        poc.eks_manifests(peer_public_key=PUB, source_cidrs=[cidr])


def test_cli_prints_kubectl_list(capsys):
    poc.main(["k3d", "--peer-public-key", PUB, "--endpoint", "nlb.example:51820"])
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "List" and out["items"][0]["kind"] == "Namespace"
