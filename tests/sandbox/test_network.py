import json

import pytest
from cmp_worker.sandbox_network import PROXY_NAME, allowlist, parse_records

from .test_deploy import FakeRunner, make_service, requirements
from .test_update import plan_with

INTENTS = [
    {"source_resource_id": "orders-api", "destination_resource_id": "orders-db", "port": 5432},
    {"source_resource_id": "orders-api", "external_host": "API.pay.example.com", "port": 443},
]


def with_intents(tmp_path, runner=None, **kw):
    base = requirements()
    reqs = type(base).model_validate(base.model_dump(mode="json") | {"network_intent": INTENTS})
    service = make_service(runner or FakeRunner())
    return service, plan_with(service, tmp_path, requirements=reqs, **kw)


def applied(service, runner, plan, keep=None):
    runner.calls.clear()
    allow = service.apply_network(plan, keep)
    stdin = next(s for a, s in runner.calls if s and "apply" in a)
    return allow, json.loads(stdin)["items"]


def kinds(items):
    return [(i["kind"], i["metadata"]["name"]) for i in items]


def test_everything_is_denied_first_and_only_dns_and_intent_edges_are_opened(tmp_path):
    runner = FakeRunner()
    service, plan = with_intents(tmp_path, runner)
    _, items = applied(service, runner, plan)
    policies = [i for i in items if i["kind"] == "NetworkPolicy"]
    deny = next(p for p in policies if p["metadata"]["name"] == "cmp-default-deny")
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    assert any(p["metadata"]["name"] == "cmp-allow-dns" for p in policies)
    api, db = plan.releases["orders-api"], plan.releases["orders-db"]
    egress = [
        p
        for p in policies
        if p["spec"].get("policyTypes") == ["Egress"]
        and "podSelector" in p["spec"]["egress"][0].get("to", [{}])[0]
    ]
    edge = [
        p
        for p in egress
        if p["spec"]["podSelector"] == {"matchLabels": {"app.kubernetes.io/instance": api}}
        and p["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"][
            "app.kubernetes.io/instance"
        ]
        == db
    ]
    assert len(edge) == 1 and edge[0]["spec"]["egress"][0]["ports"] == [
        {"protocol": "TCP", "port": 5432}
    ]
    ingress_to_db = [
        p
        for p in policies
        if p["spec"].get("policyTypes") == ["Ingress"]
        and p["spec"]["podSelector"] == {"matchLabels": {"app.kubernetes.io/instance": db}}
    ]
    assert len(ingress_to_db) == 1  # only the intent edge reaches the database


def test_a_pair_without_an_intent_gets_no_allow_policy(tmp_path):
    runner = FakeRunner()
    service, plan = with_intents(tmp_path, runner)
    _, items = applied(service, runner, plan)
    db = plan.releases["orders-db"]
    to_api = [
        i
        for i in items
        if i["kind"] == "NetworkPolicy"
        and "egress" in i["spec"]
        and i["spec"]["podSelector"] == {"matchLabels": {"app.kubernetes.io/instance": db}}
    ]
    assert to_api == []  # the database may not call anything but DNS


def test_external_hosts_reach_only_the_proxy_allow_list_never_a_direct_rule(tmp_path):
    runner = FakeRunner()
    service, plan = with_intents(tmp_path, runner)
    allow, items = applied(
        service, runner, plan, keep=[{"host": "s3.ap-northeast-2.amazonaws.com", "port": 443}]
    )
    assert allow == [
        {"host": "api.pay.example.com", "port": 443},
        {"host": "s3.ap-northeast-2.amazonaws.com", "port": 443},
    ]
    text = json.dumps([i for i in items if i["kind"] == "NetworkPolicy"])
    assert "pay.example.com" not in text and "amazonaws" not in text
    proxy_egress = next(i for i in items if i["metadata"]["name"] == "cmp-proxy-egress")
    assert "10.0.0.0/8" in proxy_egress["spec"]["egress"][0]["to"][0]["ipBlock"]["except"]
    config = next(i for i in items if i["kind"] == "ConfigMap")
    assert json.loads(config["data"]["allow.json"])["allow"] == allow
    assert "Allowlist" in config["data"]["proxy.py"]


def test_the_proxy_image_is_pinned_by_digest_and_runs_without_privileges(tmp_path):
    runner = FakeRunner()
    service, plan = with_intents(tmp_path, runner)
    _, items = applied(service, runner, plan)
    container = next(i for i in items if i["kind"] == "Deployment")["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert (
        "@sha256:" in container["image"] and container["securityContext"]["readOnlyRootFilesystem"]
    )
    assert any(
        a[-3:-1] == ["-n", plan.namespace] or PROXY_NAME in a
        for a, _ in runner.calls
        if "rollout" in a
    )


def test_applications_get_proxy_variables_only_when_the_network_is_on(tmp_path):
    runner = FakeRunner()
    _, plan = with_intents(tmp_path, runner, network=True)
    env = next(json.loads(s) for a, s in runner.calls if s and "create" in a and b"-env" in s)
    assert env["stringData"]["HTTPS_PROXY"] == f"http://{PROXY_NAME}:3128"
    assert "FEATURE_X" in env["stringData"] and ".svc" in env["stringData"]["NO_PROXY"]
    bindings = next(r for r in plan.request["resources"] if r["id"] == "orders-api")["env_bindings"]
    assert "HTTPS_PROXY" in bindings
    runner2 = FakeRunner()
    with_intents(tmp_path, runner2)
    assert not any(b"HTTPS_PROXY" in (s or b"") for _, s in runner2.calls)


def test_proxy_decisions_are_aggregated_and_noise_is_ignored():
    lines = [
        '{"decision":"ALLOW","host":"api.pay.example.com","method":"CONNECT","port":443}',
        '{"decision":"BLOCK","host":"orders-db.abc.rds.amazonaws.com","method":"CONNECT","port":5432}',
        '{"decision":"ALLOW","host":"api.pay.example.com","method":"CONNECT","port":443}',
        "not json",
        '{"decision":"MAYBE","host":"x","port":1}',
        '{"host":"x"}',
    ]
    log = "\n".join(lines)
    records = parse_records(log)
    assert [(r.decision, r.host, r.port, r.count) for r in records] == [
        ("ALLOW", "api.pay.example.com", 443, 2),
        ("BLOCK", "orders-db.abc.rds.amazonaws.com", 5432, 1),
    ]


def test_allow_list_is_sorted_and_deduplicated():
    assert allowlist(INTENTS + INTENTS, [{"host": "S3.example.com", "port": 443}]) == [
        {"host": "api.pay.example.com", "port": 443},
        {"host": "s3.example.com", "port": 443},
    ]


def test_the_external_cut_always_restores_the_proxy(tmp_path):
    runner = FakeRunner()
    service, plan = with_intents(tmp_path, runner)
    runner.calls.clear()
    with pytest.raises(RuntimeError), service.external_cut(plan):
        raise RuntimeError("boom")
    scale = [a for a, _ in runner.calls if "scale" in a]
    assert "--replicas=0" in scale[0] and "--replicas=1" in scale[1]
