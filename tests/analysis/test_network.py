import re
from pathlib import Path

import pytest
from cmp_contracts.analysis import ExternalEntry
from cmp_contracts.provisioning_v2 import NetworkIntent, RequirementsV2
from cmp_scanners.assembly import build_requirements
from cmp_scanners.aws_infra import (
    AwsInfraScan,
    EksCluster,
    ListenerInfo,
    LoadBalancerInfo,
    SecurityGroupInfo,
    SecurityGroupRule,
    VpcInfo,
)
from cmp_scanners.code import CallSite, CodeScan, EnvComparison, scan_code
from cmp_scanners.go_adapter import GoAdapter
from cmp_scanners.manifests import scan_manifests
from cmp_scanners.network import build_network, merge_network_intents

MANIFESTS = Path(__file__).parent.parent / "fixtures/analysis/assembly/manifests"
APPS, DBS = ["Shop-API"], ["shop-db"]


def rule(port, *, cidr=None, group=None, protocol="tcp", to_port=None):
    return SecurityGroupRule(
        protocol=protocol,
        from_port=port,
        to_port=port if to_port is None else to_port,
        cidrs=[cidr] if cidr else [],
        source_group_ids=[group] if group else [],
    )


def group(id, name, ingress=()):
    return SecurityGroupInfo(
        id=id, name=name, vpc_id="vpc-1", tags={"Name": name}, ingress=list(ingress), egress=[]
    )


def lb(name="shop", scheme="internet-facing", listeners=((443, "HTTPS", (8080,)),)):
    return LoadBalancerInfo(
        name=name,
        type="application",
        scheme=scheme,
        listeners=[
            ListenerInfo(port=p, protocol=pr, target_ports=list(tp)) for p, pr, tp in listeners
        ],
    )


def infra(groups=(), balancers=(), cidrs=("10.0.0.0/16",)):
    return AwsInfraScan(
        cluster=EksCluster(name="shop", version="1.30", vpc_id="vpc-1", security_group_ids=[]),
        vpcs=[VpcInfo(id="vpc-1", cidrs=list(cidrs))],
        subnets=[],
        route_tables=[],
        security_groups=list(groups),
        load_balancers=list(balancers),
        role_policies=[],
        cloudtrail=[],
        duration_seconds=1.0,
    )


def no_code():
    return CodeScan(
        call_sites=[], unscannable=[], extension_creations=[],
        env_comparison=EnvComparison(only_in_env=[], missing_from_env=[], partial=False),
    )  # fmt: skip


def db_flow():
    return infra(
        groups=[
            group("sg-app", "shop-api-sg", [rule(8080, cidr="0.0.0.0/0")]),
            group("sg-db", "shop-db-sg", [rule(5432, group="sg-app")]),
        ]
    )


def build(infra_scan, code=None, apps=APPS, dbs=DBS):
    return build_network(infra_scan, code or no_code(), application_ids=apps, database_ids=dbs)


def test_a_security_group_rule_between_two_workloads_becomes_an_intent_that_validates():
    result = build(db_flow())
    assert result.intents == [
        NetworkIntent(
            source_resource_id="Shop-API",
            destination_resource_id="shop-db",
            port=5432,
            protocol="TCP",
        )
    ]
    manifests = scan_manifests(MANIFESTS)
    base = build_requirements(manifests, no_code(), {}).requirements
    merged = RequirementsV2.model_validate(
        {
            **base.model_dump(mode="json"),
            "network_intent": [i.model_dump(mode="json") for i in result.intents],
        }
    )
    assert len(merged.network_intent) == 1


def test_intents_never_contain_target_cidrs_or_security_group_ids():
    dumped = build(db_flow(), infra_dump_code()).model_dump_json(include={"intents"})
    assert "sg-" not in dumped and not re.search(r"\d+\.\d+\.\d+\.\d+/\d+", dumped)


def infra_dump_code():
    return CodeScan(
        call_sites=[CallSite(kind="EXTERNAL_HOST", target="api.pay.example.com", file="a.go", line=1, detail="https")],
        unscannable=[], extension_creations=[],
        env_comparison=EnvComparison(only_in_env=[], missing_from_env=[], partial=False),
    )  # fmt: skip


def test_an_internet_facing_load_balancer_becomes_an_external_entry_and_cidrs_are_recorded():
    result = build(
        infra(
            groups=db_flow().security_groups, balancers=[lb()], cidrs=("10.1.0.0/16", "10.0.0.0/16")
        )
    )
    assert result.external_entries == [
        ExternalEntry(
            protocol="HTTPS",
            port=443,
            target_resource_id="Shop-API",
            target_port=8080,
            source_service="ALB",
        )
    ]
    assert result.source_cidrs == ["10.0.0.0/16", "10.1.0.0/16"]


def test_internal_load_balancers_and_non_http_listeners_are_not_external_entries():
    result = build(
        infra(balancers=[lb(scheme="internal"), lb(name="n", listeners=((5432, "TCP", (5432,)),))])
    )
    assert result.external_entries == []


def test_without_a_load_balancer_an_internet_open_security_group_gives_the_entry():
    result = build(db_flow())
    assert result.external_entries == [
        ExternalEntry(
            protocol="HTTP",
            port=8080,
            target_resource_id="Shop-API",
            target_port=8080,
            source_service="SECURITY_GROUP",
        )
    ]


def test_a_load_balancer_entry_replaces_the_security_group_one():
    result = build(infra(groups=db_flow().security_groups, balancers=[lb()]))
    assert [e.source_service for e in result.external_entries] == ["ALB"]


def test_ambiguous_entry_targets_are_noted_not_guessed():
    result = build(infra(balancers=[lb()]), apps=["a-api", "b-api"])
    assert result.external_entries == [] and any(
        "EXTERNAL_ENTRY_TARGET_AMBIGUOUS" in n for n in result.notes
    )


def test_broad_rules_and_unmapped_groups_are_noted_and_skipped():
    groups = [
        group("sg-app", "shop-api-sg"),
        group(
            "sg-db",
            "shop-db-sg",
            [rule(0, group="sg-app", to_port=65535), rule(None, group="sg-app", protocol="-1")],
        ),
        group("sg-x", "mystery", [rule(5432, group="sg-app")]),
    ]
    result = build(infra(groups=groups))
    assert result.intents == []
    notes = " ".join(result.notes)
    assert "BROAD_SECURITY_GROUP_RULE" in notes and "SECURITY_GROUP_UNMAPPED" in notes


def test_literal_external_hosts_from_code_become_external_host_intents_per_app():
    code = CodeScan(
        call_sites=[
            CallSite(kind="EXTERNAL_HOST", target="api.pay.example.com", file="a.go", line=1, detail="https"),
            CallSite(kind="EXTERNAL_HOST", target="billing.example.net:8443", file="a.go", line=2, detail="http"),
            CallSite(kind="EXTERNAL_HOST", target="plain.example.org", file="a.go", line=3, detail="http"),
            CallSite(kind="EXTERNAL_HOST", target="orders-db.orders.svc.cluster.local", file="a.go", line=4, detail="http"),
            CallSite(kind="EXTERNAL_HOST", target="api.pay.example.com", file="b.go", line=9, detail="https"),
        ],
        unscannable=[], extension_creations=[],
        env_comparison=EnvComparison(only_in_env=[], missing_from_env=[], partial=False),
    )  # fmt: skip
    result = build(infra(), code)
    assert {(i.external_host, i.port) for i in result.intents} == {
        ("api.pay.example.com", 443),
        ("billing.example.net", 8443),
        ("plain.example.org", 80),
    }
    assert {i.source_resource_id for i in result.intents} == {"Shop-API"}


def test_the_go_adapter_output_flows_into_external_host_intents(tmp_path):
    (tmp_path / "main.go").write_text(
        'package main\nvar _ = http.Get("https://api.pay.example.com/v1")\n'
    )
    result = build(infra(), scan_code(tmp_path, {}, [GoAdapter()]))
    assert [(i.external_host, i.port) for i in result.intents] == [("api.pay.example.com", 443)]


def test_merging_intent_groups_removes_duplicates_and_keeps_order():
    a = NetworkIntent(source_resource_id="x", destination_resource_id="y", port=5432)
    b = NetworkIntent(source_resource_id="x", external_host="h.example.com", port=443)
    assert merge_network_intents([a], [b, a]) == [a, b]


@pytest.mark.parametrize("apps", [[], ["Shop-API"]])
def test_without_applications_there_are_no_external_host_intents(apps):
    result = build(infra(), infra_dump_code(), apps=apps)
    assert (len(result.intents) == 1) is bool(apps)


def test_without_an_aws_scan_only_code_based_external_hosts_are_derived():
    result = build_network(None, infra_dump_code(), application_ids=APPS, database_ids=DBS)
    assert [(i.external_host, i.port) for i in result.intents] == [("api.pay.example.com", 443)]
    assert result.external_entries == [] and result.source_cidrs == []
    assert any("INFRA_NOT_COLLECTED" in n for n in result.notes)
