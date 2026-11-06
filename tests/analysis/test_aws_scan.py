import dataclasses
import inspect
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from cmp_scanners import aws_infra
from cmp_scanners.aws_infra import (
    ALLOWED_OPERATIONS,
    AwsClients,
    AwsScanError,
    get_session,
    required_iam_actions,
    scan_aws_infra,
    scan_policy_document,
)

REGION = "ap-northeast-2"
ACCOUNT = "123456789012"
NOW = datetime(2026, 11, 5, tzinfo=UTC)
LB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/shop/abc"
OTHER_LB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/other/def"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/shop-tg/123"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/shop-api"
POLICY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/shop-read"
LB_DNS = f"shop-123.{REGION}.elb.amazonaws.com"


def policy_json(action, resource):
    document = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": action, "Resource": resource}],
    }
    return quote(json.dumps(document))  # the IAM wire format; botocore decodes it for callers


def client(service):
    return boto3.client(
        service,
        region_name=REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(retries={"max_attempts": 1}),
    )


def build_clients():
    clients = AwsClients(*(client(s) for s in ("eks", "ec2", "elbv2", "iam", "cloudtrail")))
    stubs = {n: Stubber(getattr(clients, n)) for n in ("eks", "ec2", "elbv2", "iam", "cloudtrail")}
    return clients, stubs


def rule(port, *, cidr=None, group=None):
    return {
        "IpProtocol": "tcp",
        "FromPort": port,
        "ToPort": port,
        "IpRanges": [{"CidrIp": cidr}] if cidr else [],
        "UserIdGroupPairs": [{"GroupId": group}] if group else [],
    }


def sg(group_id, name, ingress):
    return {
        "GroupId": group_id,
        "GroupName": name,
        "VpcId": "vpc-1",
        "Description": "d",
        "OwnerId": ACCOUNT,
        "Tags": [{"Key": "Name", "Value": name}],
        "IpPermissions": ingress,
        "IpPermissionsEgress": [],
    }


def stub_happy(stubs, *, cloudtrail_events=None, listener_extra=None):
    stubs["eks"].add_response(
        "describe_cluster",
        {
            "cluster": {
                "name": "shop",
                "version": "1.30",
                "resourcesVpcConfig": {"vpcId": "vpc-1", "securityGroupIds": ["sg-cluster"]},
            }
        },
    )
    ec2 = stubs["ec2"]
    ec2.add_response(
        "describe_vpcs",
        {
            "Vpcs": [
                {
                    "VpcId": "vpc-1",
                    "CidrBlock": "10.0.0.0/16",
                    "CidrBlockAssociationSet": [
                        {"CidrBlock": "10.0.0.0/16", "CidrBlockState": {"State": "associated"}},
                        {"CidrBlock": "10.1.0.0/16", "CidrBlockState": {"State": "associated"}},
                    ],
                }
            ]
        },
    )
    ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": "subnet-1", "VpcId": "vpc-1", "CidrBlock": "10.0.1.0/24"}]},
    )
    ec2.add_response(
        "describe_route_tables",
        {
            "RouteTables": [
                {
                    "RouteTableId": "rtb-1",
                    "VpcId": "vpc-1",
                    "Routes": [
                        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"},
                        {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"},
                    ],
                }
            ]
        },
    )
    ec2.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                sg("sg-app", "shop-api-sg", [rule(8080, cidr="0.0.0.0/0")]),
                sg("sg-db", "shop-db-sg", [rule(5432, group="sg-app")]),
            ]
        },
    )
    elb = stubs["elbv2"]
    elb.add_response(
        "describe_load_balancers",
        {
            "LoadBalancers": [
                {
                    "LoadBalancerArn": LB_ARN,
                    "LoadBalancerName": "shop",
                    "VpcId": "vpc-1",
                    "Scheme": "internet-facing",
                    "Type": "application",
                    "DNSName": LB_DNS,
                },
                {
                    "LoadBalancerArn": OTHER_LB_ARN,
                    "LoadBalancerName": "other",
                    "VpcId": "vpc-9",
                    "Scheme": "internal",
                    "Type": "application",
                },
            ]
        },
    )
    elb.add_response(
        "describe_listeners",
        {
            "Listeners": [
                {
                    "ListenerArn": LB_ARN.replace("loadbalancer", "listener"),
                    "LoadBalancerArn": LB_ARN,
                    "Port": 443,
                    "Protocol": "HTTPS",
                    "DefaultActions": [{"Type": "forward", "TargetGroupArn": TG_ARN}],
                    **(listener_extra or {}),
                }
            ]
        },
    )
    elb.add_response(
        "describe_target_groups",
        {
            "TargetGroups": [
                {
                    "TargetGroupArn": TG_ARN,
                    "TargetGroupName": "shop-tg",
                    "Port": 8080,
                    "Protocol": "HTTP",
                    "TargetType": "ip",
                }
            ]
        },
    )
    iam = stubs["iam"]
    iam.add_response(
        "list_attached_role_policies",
        {"AttachedPolicies": [{"PolicyName": "shop-read", "PolicyArn": POLICY_ARN}]},
    )
    iam.add_response("list_role_policies", {"PolicyNames": ["inline1"]})
    iam.add_response("get_policy", {"Policy": {"DefaultVersionId": "v1"}})
    iam.add_response(
        "get_policy_version",
        {
            "PolicyVersion": {
                "Document": policy_json("sqs:SendMessage", f"arn:aws:sqs:{REGION}:{ACCOUNT}:q"),
                "VersionId": "v1",
            }
        },
    )
    iam.add_response(
        "get_role_policy",
        {
            "RoleName": "shop-api",
            "PolicyName": "inline1",
            "PolicyDocument": policy_json(["s3:GetObject", "s3:PutObject"], "*"),
        },
    )
    events = cloudtrail_events or [
        {
            "EventName": "RunInstances",
            "EventSource": "ec2.amazonaws.com",
            "Username": "alice@example.com",
        },
        {"EventName": "RunInstances", "EventSource": "ec2.amazonaws.com", "Username": "bob"},
        {
            "EventName": "CreateBucket",
            "EventSource": "s3.amazonaws.com",
            "Username": "alice@example.com",
        },
    ]
    stubs["cloudtrail"].add_response("lookup_events", {"Events": events})


def activate(stubs):
    for stub in stubs.values():
        stub.activate()


def scan(clients, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("now", NOW)
    return scan_aws_infra(clients, cluster_name="shop", role_arns=[ROLE_ARN], **kw)


def test_the_scan_reads_cluster_network_load_balancers_roles_and_trail():
    clients, stubs = build_clients()
    stub_happy(stubs)
    activate(stubs)
    result = scan(clients)
    assert (result.cluster.name, result.cluster.vpc_id) == ("shop", "vpc-1")
    assert result.vpcs[0].cidrs == ["10.0.0.0/16", "10.1.0.0/16"]
    assert [s.id for s in result.subnets] == ["subnet-1"]
    assert [(t.id, t.has_internet_route) for t in result.route_tables] == [("rtb-1", True)]
    db = next(g for g in result.security_groups if g.id == "sg-db")
    assert db.ingress[0].source_group_ids == ["sg-app"] and (
        db.ingress[0].from_port,
        db.ingress[0].protocol,
    ) == (5432, "tcp")
    (lb,) = result.load_balancers
    assert (lb.name, lb.scheme) == ("shop", "internet-facing")
    assert [(l.port, l.protocol, l.target_ports) for l in lb.listeners] == [(443, "HTTPS", [8080])]
    (role,) = result.role_policies
    assert (
        role.actions == ["s3:GetObject", "s3:PutObject", "sqs:SendMessage"]
        and role.wildcard_resources is True
    )
    assert {(e.event_source, e.event_name, e.count) for e in result.cloudtrail} == {
        ("ec2.amazonaws.com", "RunInstances", 2),
        ("s3.amazonaws.com", "CreateBucket", 1),
    }
    assert "alice" not in result.model_dump_json() and "bob" not in result.model_dump_json()


def test_only_resources_of_the_cluster_vpc_are_kept():
    clients, stubs = build_clients()
    stub_happy(stubs)
    activate(stubs)
    assert [lb.name for lb in scan(clients).load_balancers] == ["shop"]


def test_permission_denied_fails_without_retry_and_names_the_permission():
    clients, stubs = build_clients()
    stubs["eks"].add_response(
        "describe_cluster", {"cluster": {"name": "shop", "resourcesVpcConfig": {"vpcId": "vpc-1"}}}
    )
    stubs["ec2"].add_response(
        "describe_vpcs", {"Vpcs": [{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16"}]}
    )
    stubs["ec2"].add_response("describe_subnets", {"Subnets": []})
    stubs["ec2"].add_response("describe_route_tables", {"RouteTables": []})
    stubs["ec2"].add_client_error(
        "describe_security_groups",
        service_error_code="UnauthorizedOperation",
        service_message="secret detail from aws",
    )
    activate(stubs)
    with pytest.raises(AwsScanError) as exc:
        scan(clients)
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.permission == "ec2:DescribeSecurityGroups"
    assert "secret detail" not in str(exc.value)


def test_throttling_is_retried_with_backoff_and_then_succeeds():
    clients, stubs = build_clients()
    stubs["eks"].add_client_error("describe_cluster", service_error_code="ThrottlingException")
    stubs["eks"].add_client_error("describe_cluster", service_error_code="ThrottlingException")
    stub_happy(stubs)  # adds the successful describe_cluster after the two errors
    activate(stubs)
    sleeps = []
    result = scan(clients, sleep=sleeps.append)
    assert result.cluster.name == "shop" and sleeps == [0.5, 1.0]


def test_persistent_throttling_ends_as_throttled():
    clients, stubs = build_clients()
    for _ in range(4):
        stubs["eks"].add_client_error("describe_cluster", service_error_code="Throttling")
    activate(stubs)
    with pytest.raises(AwsScanError) as exc:
        scan(clients)
    assert exc.value.code == "THROTTLED"


def test_other_aws_errors_are_reported_without_the_aws_message():
    clients, stubs = build_clients()
    stubs["eks"].add_client_error(
        "describe_cluster",
        service_error_code="ResourceNotFoundException",
        service_message="no cluster named secret-name",
    )
    activate(stubs)
    with pytest.raises(AwsScanError) as exc:
        scan(clients)
    assert exc.value.code == "AWS_ERROR" and "secret-name" not in str(exc.value)


def test_only_allow_listed_read_only_operations_are_called():
    clients, stubs = build_clients()
    stub_happy(stubs)
    activate(stubs)
    called = set()
    for service in ("eks", "ec2", "elbv2", "iam", "cloudtrail"):
        getattr(clients, service).meta.events.register_first(
            "before-call.*.*",
            lambda model, **_: called.add(model.name),
        )
    scan(clients)
    allowed = {op.operation for op in ALLOWED_OPERATIONS}
    assert called and called <= allowed
    assert not any(
        re.search(r"Secret|Parameter|Decrypt|Create|Delete|Put|Update", name) for name in called
    )


def test_the_scanner_source_never_touches_secret_stores():
    source = inspect.getsource(aws_infra).lower()
    for forbidden in (
        "secretsmanager",
        "get_secret_value",
        "ssm",
        "get_parameter",
        "decrypt",
        "kms",
    ):
        assert forbidden not in source


def test_the_permission_list_is_read_only_and_has_no_wildcards():
    actions = required_iam_actions()
    assert actions == sorted(set(actions)) and actions
    assert all(re.fullmatch(r"[a-z0-9]+:(Describe|List|Get|Lookup)[A-Za-z]+", a) for a in actions)
    assert "ec2:DescribeSecurityGroups" in actions and "cloudtrail:LookupEvents" in actions
    policy = scan_policy_document()
    assert (
        policy["Statement"][0]["Effect"] == "Allow" and policy["Statement"][0]["Action"] == actions
    )


def test_the_scan_duration_is_measured():
    clients, stubs = build_clients()
    stub_happy(stubs)
    activate(stubs)
    ticks = iter([100.0, 107.5])
    assert scan(clients, clock=lambda: next(ticks)).duration_seconds == 7.5


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "AKIAABCDEFGHIJKLMNOP",
        "ASIAABCDEFGHIJKLMNOP",
        "has space",
        "a" * 80,
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    ],
)
def test_session_refs_must_be_explicit_profile_names_never_keys(ref):
    with pytest.raises(AwsScanError) as exc:
        get_session(ref)
    assert exc.value.code == "INVALID_CREDENTIAL_REF"


def test_a_named_profile_makes_a_session(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text(f"[profile demo-source]\nregion = {REGION}\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    assert get_session("demo-source").profile_name == "demo-source"


# ------------------------------------------------------------- CloudTrail limits (fix)


class EndlessTrail:
    """A LookupEvents fake that always has another page of 50 events."""

    def __init__(self):
        self.calls = []

    def lookup_events(self, **params):
        self.calls.append(params)
        events = [{"EventSource": "s3.amazonaws.com", "EventName": "GetObject"}] * 50
        return {"Events": events, "NextToken": f"t{len(self.calls)}"}


class FailingTrail:
    def __init__(self, code):
        self.code = code
        self.calls = 0

    def lookup_events(self, **params):
        self.calls += 1
        raise ClientError(
            {"Error": {"Code": self.code, "Message": "secret aws text"}}, "LookupEvents"
        )


def with_trail(trail):
    clients, stubs = build_clients()
    stub_happy(stubs)
    stubs.pop("cloudtrail")
    activate(stubs)
    return dataclasses.replace(clients, cloudtrail=trail)


def test_cloudtrail_stops_paging_at_the_event_limit_and_paces_its_pages():
    trail = EndlessTrail()
    sleeps = []
    result = scan(with_trail(trail), sleep=sleeps.append)
    assert len(trail.calls) == 10  # ceil(500 / 50)
    assert sleeps == [0.5] * 9  # between pages, never after the last one
    assert result.cloudtrail_status == "COLLECTED" and result.cloudtrail_error is None
    assert [(e.event_name, e.count) for e in result.cloudtrail] == [("GetObject", 500)]


def test_a_small_event_limit_asks_for_one_short_page():
    trail = EndlessTrail()
    scan(with_trail(trail), cloudtrail_max_events=20)
    assert len(trail.calls) == 1 and trail.calls[0]["MaxResults"] == 20


@pytest.mark.parametrize(
    ("aws_code", "code"),
    [
        ("ThrottlingException", "THROTTLED"),
        ("AccessDeniedException", "PERMISSION_DENIED"),
        ("InvalidLookupAttributesException", "AWS_ERROR"),
    ],
)
def test_a_failed_cloudtrail_read_keeps_the_infrastructure_and_is_marked(aws_code, code):
    trail = FailingTrail(aws_code)
    result = scan(with_trail(trail))
    assert result.cloudtrail == []
    assert (result.cloudtrail_status, result.cloudtrail_error) == ("NOT_COLLECTED", code)
    assert result.vpcs and result.subnets and result.security_groups and result.load_balancers
    assert result.role_policies
    assert "secret aws text" not in result.model_dump_json()
    assert trail.calls == (4 if code == "THROTTLED" else 1)


def test_scans_stored_before_the_status_field_read_as_collected():
    clients, stubs = build_clients()
    stub_happy(stubs)
    activate(stubs)
    stored = scan(clients).model_dump(mode="json")
    stored.pop("cloudtrail_status")
    stored.pop("cloudtrail_error")
    assert aws_infra.AwsInfraScan.model_validate(stored).cloudtrail_status == "COLLECTED"
