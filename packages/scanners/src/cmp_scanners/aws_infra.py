"""Read-only scan of the source AWS environment (AV-005).

Only the operations in `ALLOWED_OPERATIONS` are ever called; they are all describe, list or
lookup calls. Errors carry a stable code and, for denied calls, the IAM action that is
missing, never AWS's own message. Throttling is retried with backoff; nothing else is.
"""

import json
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound
from pydantic import BaseModel, ConfigDict, Field

_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ACCESS_KEY = re.compile(r"^(?:AKIA|ASIA)[A-Z0-9]{16}$")
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/.+$")
_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedAccess"}
_THROTTLED = {
    "Throttling", "ThrottlingException", "RequestLimitExceeded",
    "TooManyRequestsException", "ThrottledException", "RequestThrottled",
}  # fmt: skip
_ATTEMPTS = 4
# LookupEvents allows 2 requests per second per account; one page every 0.5 s stays inside it.
_CLOUDTRAIL_PAGE_SIZE = 50
_CLOUDTRAIL_PAGE_INTERVAL = 0.5
# A CloudTrail read that fails with these leaves the rest of the scan intact (NOT_COLLECTED).
_CLOUDTRAIL_SOFT_ERRORS = {"THROTTLED", "PERMISSION_DENIED", "AWS_ERROR"}


@dataclass(frozen=True)
class AwsOperation:
    client: str
    method: str
    operation: str
    iam_action: str


ALLOWED_OPERATIONS = (
    AwsOperation("eks", "describe_cluster", "DescribeCluster", "eks:DescribeCluster"),
    AwsOperation("ec2", "describe_vpcs", "DescribeVpcs", "ec2:DescribeVpcs"),
    AwsOperation("ec2", "describe_subnets", "DescribeSubnets", "ec2:DescribeSubnets"),
    AwsOperation("ec2", "describe_route_tables", "DescribeRouteTables", "ec2:DescribeRouteTables"),
    AwsOperation(
        "ec2", "describe_security_groups", "DescribeSecurityGroups", "ec2:DescribeSecurityGroups"
    ),
    AwsOperation(
        "elbv2", "describe_load_balancers", "DescribeLoadBalancers",
        "elasticloadbalancing:DescribeLoadBalancers",
    ),
    AwsOperation(
        "elbv2", "describe_listeners", "DescribeListeners", "elasticloadbalancing:DescribeListeners"
    ),
    AwsOperation(
        "elbv2", "describe_target_groups", "DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetGroups",
    ),
    AwsOperation(
        "iam", "list_attached_role_policies", "ListAttachedRolePolicies",
        "iam:ListAttachedRolePolicies",
    ),
    AwsOperation("iam", "list_role_policies", "ListRolePolicies", "iam:ListRolePolicies"),
    AwsOperation("iam", "get_role_policy", "GetRolePolicy", "iam:GetRolePolicy"),
    AwsOperation("iam", "get_policy", "GetPolicy", "iam:GetPolicy"),
    AwsOperation("iam", "get_policy_version", "GetPolicyVersion", "iam:GetPolicyVersion"),
    AwsOperation("cloudtrail", "lookup_events", "LookupEvents", "cloudtrail:LookupEvents"),
    AwsOperation("cloudfront", "list_distributions", "ListDistributions", "cloudfront:ListDistributions"),
)  # fmt: skip
_BY_METHOD = {(op.client, op.method): op for op in ALLOWED_OPERATIONS}


def required_iam_actions() -> list[str]:
    return sorted({op.iam_action for op in ALLOWED_OPERATIONS})


def scan_policy_document() -> dict[str, Any]:
    """The read-only IAM policy the scan needs, to hand to whoever issues the scan role."""
    return {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": required_iam_actions(), "Resource": "*"}],
    }


class AwsScanError(Exception):
    def __init__(self, code: str, *, permission: str | None = None, operation: str | None = None):
        detail = ", ".join(x for x in (code, permission, operation) if x)
        super().__init__(detail)
        self.code = code
        self.permission = permission
        self.operation = operation


@dataclass(frozen=True)
class AwsClients:
    eks: Any
    ec2: Any
    elbv2: Any
    iam: Any
    cloudtrail: Any
    cloudfront: Any = None  # 28-A: optional, so a scan with five clients still works


def get_session(credential_ref: str, *, region: str | None = None) -> boto3.Session:
    """`credential_ref` names a local profile; keys and the default profile are never accepted."""
    if _ACCESS_KEY.fullmatch(credential_ref) or not _PROFILE.fullmatch(credential_ref):
        raise AwsScanError("INVALID_CREDENTIAL_REF")
    try:
        return boto3.Session(profile_name=credential_ref, region_name=region)
    except ProfileNotFound:
        raise AwsScanError("PROFILE_NOT_FOUND") from None


def make_clients(session: boto3.Session) -> AwsClients:
    config = Config(retries={"max_attempts": 1}, connect_timeout=5, read_timeout=30)
    return AwsClients(
        *(
            session.client(name, config=config)
            for name in ("eks", "ec2", "elbv2", "iam", "cloudtrail", "cloudfront")
        )
    )


# ------------------------------------------------------------------------------ models


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EksCluster(_Model):
    name: str
    version: str | None
    vpc_id: str
    security_group_ids: list[str]


class VpcInfo(_Model):
    id: str
    cidrs: list[str]


class SubnetInfo(_Model):
    id: str
    vpc_id: str
    cidr: str | None


class RouteTableInfo(_Model):
    id: str
    vpc_id: str
    has_internet_route: bool


class SecurityGroupRule(_Model):
    protocol: str
    from_port: int | None
    to_port: int | None
    cidrs: list[str]
    source_group_ids: list[str]


class SecurityGroupInfo(_Model):
    id: str
    name: str
    vpc_id: str
    tags: dict[str, str]
    ingress: list[SecurityGroupRule]
    egress: list[SecurityGroupRule]


class ListenerInfo(_Model):
    port: int
    protocol: str
    target_ports: list[int]
    # 28-A: how the listener protects traffic (a certificate and policy, or a redirect to HTTPS).
    has_certificate: bool = False
    ssl_policy: str | None = None
    redirects_to_https: bool = False


class LoadBalancerInfo(_Model):
    name: str
    type: str
    scheme: str
    listeners: list[ListenerInfo]
    dns_name: str | None = None  # 28-A: lets a CloudFront origin be matched to this balancer


class CloudFrontInfo(_Model):
    """A distribution whose origin is a load balancer of the cluster VPC (28-A)."""

    id: str
    domain_name: str
    aliases: list[str]
    enabled: bool
    viewer_protocol_policies: list[str]  # allow-all | redirect-to-https | https-only
    origin_domains: list[str]  # the load balancers' DNS names that are origins


class RolePolicyInfo(_Model):
    role_arn: str
    actions: list[str]
    wildcard_resources: bool


class CloudTrailEventSummary(_Model):
    event_source: str
    event_name: str
    count: int


class AwsInfraScan(_Model):
    cluster: EksCluster
    vpcs: list[VpcInfo]
    subnets: list[SubnetInfo]
    route_tables: list[RouteTableInfo]
    security_groups: list[SecurityGroupInfo]
    load_balancers: list[LoadBalancerInfo]
    role_policies: list[RolePolicyInfo]
    cloudtrail: list[CloudTrailEventSummary]
    duration_seconds: float
    cloudfront: list[CloudFrontInfo] = Field(default_factory=list)
    # NOT_REQUESTED: no client or no load balancer to match. DENIED: the role predates the
    # permission (the scan goes on). Never read it as "no distribution" unless COLLECTED.
    cloudfront_status: Literal["COLLECTED", "DENIED", "NOT_REQUESTED"] = "NOT_REQUESTED"
    # NOT_COLLECTED: LookupEvents failed (code in `cloudtrail_error`), so `cloudtrail` is empty
    # and must not be read as "no calls". Older stored scans default to COLLECTED.
    cloudtrail_status: Literal["COLLECTED", "NOT_COLLECTED"] = "COLLECTED"
    cloudtrail_error: str | None = None


# ------------------------------------------------------------------------------- calls


class _Caller:
    def __init__(self, clients: AwsClients, sleep: Callable[[float], None]):
        self._clients = clients
        self._sleep = sleep

    def __call__(self, client: str, method: str, **params: Any) -> dict[str, Any]:
        op = _BY_METHOD[(client, method)]
        function = getattr(getattr(self._clients, client), method)
        delay = 0.5
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                return function(**params)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in _DENIED:
                    raise AwsScanError(
                        "PERMISSION_DENIED", permission=op.iam_action, operation=op.operation
                    ) from None
                if code in _THROTTLED:
                    if attempt == _ATTEMPTS:
                        raise AwsScanError("THROTTLED", operation=op.operation) from None
                    self._sleep(delay)
                    delay *= 2
                    continue
                raise AwsScanError("AWS_ERROR", operation=op.operation) from None
            except NoCredentialsError:
                raise AwsScanError("NO_CREDENTIALS", operation=op.operation) from None
            except BotoCoreError:
                raise AwsScanError("CONNECTION_ERROR", operation=op.operation) from None
        raise AwsScanError("THROTTLED", operation=op.operation)  # pragma: no cover

    def pages(
        self,
        client: str,
        method: str,
        key: str,
        token: str = "NextToken",
        *,
        limit: int | None = None,
        interval: float = 0.0,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """All items under `key`, following `NextToken` (or `Marker`/`NextMarker`).

        With `limit`, no further page is requested once that many items are in hand (the
        result is cut to `limit`). With `interval`, that many seconds pass between pages."""
        request_token, response_token = (
            (token, token) if token == "NextToken" else ("Marker", "NextMarker")
        )
        items: list[dict[str, Any]] = []
        while True:
            response = self(client, method, **params)
            items.extend(response.get(key, []))
            following = response.get(response_token)
            if limit is not None and len(items) >= limit:
                return items[:limit]
            if not following:
                return items
            if interval:
                self._sleep(interval)
            params = {**params, request_token: following}


def _tags(raw: Sequence[dict[str, str]]) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in raw if "Key" in t and "Value" in t}


def _rules(permissions: Sequence[dict[str, Any]]) -> list[SecurityGroupRule]:
    return [
        SecurityGroupRule(
            protocol=p.get("IpProtocol", ""),
            from_port=p.get("FromPort"),
            to_port=p.get("ToPort"),
            cidrs=[r["CidrIp"] for r in p.get("IpRanges", []) if "CidrIp" in r],
            source_group_ids=[
                g["GroupId"] for g in p.get("UserIdGroupPairs", []) if "GroupId" in g
            ],
        )
        for p in permissions
    ]


def _policy_actions(document: dict[str, Any] | str) -> tuple[set[str], bool]:
    if isinstance(document, str):  # raw IAM wire format: URL-encoded JSON
        document = json.loads(unquote(document))
    statements = document.get("Statement", [])
    statements = [statements] if isinstance(statements, dict) else statements
    actions: set[str] = set()
    wildcard = False
    for statement in statements:
        if statement.get("Effect") != "Allow":
            continue
        raw = statement.get("Action", [])
        actions.update([raw] if isinstance(raw, str) else raw)
        resources = statement.get("Resource", [])
        wildcard = wildcard or "*" in ([resources] if isinstance(resources, str) else resources)
    return actions, wildcard


def _cloudfront(call: _Caller, balancers: Sequence[LoadBalancerInfo]) -> list[CloudFrontInfo]:
    """Distributions whose origin is one of the balancers (28-A). Lists every page."""
    names = {lb.dns_name.lower(): lb.dns_name for lb in balancers if lb.dns_name}
    found: list[CloudFrontInfo] = []
    marker: str | None = None
    while True:
        listing = call(
            "cloudfront", "list_distributions", **({"Marker": marker} if marker else {})
        ).get("DistributionList", {})
        for item in listing.get("Items") or []:
            origins = (item.get("Origins") or {}).get("Items") or []
            matched = sorted(
                {
                    names[o["DomainName"].lower()]
                    for o in origins
                    if o["DomainName"].lower() in names
                }
            )
            if not matched:
                continue
            behaviors = (item.get("CacheBehaviors") or {}).get("Items") or []
            policies = {
                item["DefaultCacheBehavior"]["ViewerProtocolPolicy"],
                *(b["ViewerProtocolPolicy"] for b in behaviors),
            }
            found.append(
                CloudFrontInfo(
                    id=item["Id"],
                    domain_name=item["DomainName"],
                    aliases=list((item.get("Aliases") or {}).get("Items") or []),
                    enabled=bool(item.get("Enabled")),
                    viewer_protocol_policies=sorted(policies),
                    origin_domains=matched,
                )
            )
        marker = listing.get("NextMarker")
        if not (listing.get("IsTruncated") and marker):
            return sorted(found, key=lambda d: d.id)


def _read_cloudfront(
    call: _Caller, clients: AwsClients, balancers: Sequence[LoadBalancerInfo]
) -> tuple[list[CloudFrontInfo], Literal["COLLECTED", "DENIED", "NOT_REQUESTED"]]:
    """Best effort: a role created before this permission existed still scans (status DENIED)."""
    if clients.cloudfront is None or not any(lb.dns_name for lb in balancers):
        return [], "NOT_REQUESTED"
    try:
        return _cloudfront(call, balancers), "COLLECTED"
    except AwsScanError as error:
        if error.code != "PERMISSION_DENIED":
            raise
        return [], "DENIED"


def _role_policy(call: _Caller, role_arn: str) -> RolePolicyInfo:
    if not _ROLE_ARN.fullmatch(role_arn):
        raise AwsScanError("INVALID_ROLE_ARN")
    role = role_arn.rsplit("/", 1)[-1]
    actions: set[str] = set()
    wildcard = False
    attached_policies = call.pages(
        "iam", "list_attached_role_policies", "AttachedPolicies", "Marker", RoleName=role
    )
    inline_names = call.pages("iam", "list_role_policies", "PolicyNames", "Marker", RoleName=role)
    for attached in attached_policies:
        arn = attached["PolicyArn"]
        version = call("iam", "get_policy", PolicyArn=arn)["Policy"]["DefaultVersionId"]
        document = call("iam", "get_policy_version", PolicyArn=arn, VersionId=version)[
            "PolicyVersion"
        ]["Document"]
        found, star = _policy_actions(document)
        actions |= found
        wildcard = wildcard or star
    for name in inline_names:
        document = call("iam", "get_role_policy", RoleName=role, PolicyName=name)["PolicyDocument"]
        found, star = _policy_actions(document)
        actions |= found
        wildcard = wildcard or star
    return RolePolicyInfo(role_arn=role_arn, actions=sorted(actions), wildcard_resources=wildcard)


def _read_cloudtrail(
    call: _Caller, *, end: datetime, days: int, max_events: int
) -> tuple[list[CloudTrailEventSummary], str | None]:
    """The newest `max_events` management events, at most ceil(max_events / 50) pages, paced
    under the LookupEvents rate. A throttled, denied or failed read gives (`[]`, code) so the
    infrastructure already read is kept."""
    if max_events <= 0:
        return [], None
    try:
        events = call.pages(
            "cloudtrail",
            "lookup_events",
            "Events",
            limit=max_events,
            interval=_CLOUDTRAIL_PAGE_INTERVAL,
            StartTime=end - timedelta(days=days),
            EndTime=end,
            MaxResults=min(_CLOUDTRAIL_PAGE_SIZE, max_events),
        )
    except AwsScanError as error:
        if error.code not in _CLOUDTRAIL_SOFT_ERRORS:
            raise
        return [], error.code
    counts: Counter[tuple[str, str]] = Counter(
        (event.get("EventSource", ""), event.get("EventName", "")) for event in events
    )
    return [
        CloudTrailEventSummary(event_source=s, event_name=n, count=c)
        for (s, n), c in sorted(counts.items())
    ], None


def scan_aws_infra(
    clients: AwsClients,
    *,
    cluster_name: str,
    role_arns: Sequence[str] = (),
    cloudtrail_days: int = 7,
    cloudtrail_max_events: int = 500,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
) -> AwsInfraScan:
    started = clock()
    call = _Caller(clients, sleep)
    described = call("eks", "describe_cluster", name=cluster_name)["cluster"]
    vpc_config = described.get("resourcesVpcConfig", {})
    vpc_id = vpc_config["vpcId"]
    cluster = EksCluster(
        name=described["name"],
        version=described.get("version"),
        vpc_id=vpc_id,
        security_group_ids=list(vpc_config.get("securityGroupIds", [])),
    )
    by_vpc = [{"Name": "vpc-id", "Values": [vpc_id]}]

    vpcs = []
    for vpc in call.pages("ec2", "describe_vpcs", "Vpcs", VpcIds=[vpc_id]):
        cidrs = [vpc["CidrBlock"]] if "CidrBlock" in vpc else []
        cidrs += [
            a["CidrBlock"] for a in vpc.get("CidrBlockAssociationSet", []) if "CidrBlock" in a
        ]
        vpcs.append(VpcInfo(id=vpc["VpcId"], cidrs=list(dict.fromkeys(cidrs))))
    subnets = [
        SubnetInfo(id=s["SubnetId"], vpc_id=s["VpcId"], cidr=s.get("CidrBlock"))
        for s in call.pages("ec2", "describe_subnets", "Subnets", Filters=by_vpc)
    ]
    route_tables = [
        RouteTableInfo(
            id=t["RouteTableId"],
            vpc_id=t["VpcId"],
            has_internet_route=any(
                r.get("GatewayId", "").startswith("igw-") for r in t.get("Routes", [])
            ),
        )
        for t in call.pages("ec2", "describe_route_tables", "RouteTables", Filters=by_vpc)
    ]
    groups = [
        SecurityGroupInfo(
            id=g["GroupId"],
            name=g["GroupName"],
            vpc_id=g["VpcId"],
            tags=_tags(g.get("Tags", [])),
            ingress=_rules(g.get("IpPermissions", [])),
            egress=_rules(g.get("IpPermissionsEgress", [])),
        )
        for g in call.pages("ec2", "describe_security_groups", "SecurityGroups", Filters=by_vpc)
    ]

    balancers = []
    for lb in call.pages("elbv2", "describe_load_balancers", "LoadBalancers", "Marker"):
        if lb.get("VpcId") != vpc_id:
            continue
        arn = lb["LoadBalancerArn"]
        raw_listeners = call.pages(
            "elbv2", "describe_listeners", "Listeners", "Marker", LoadBalancerArn=arn
        )
        ports = {
            t["TargetGroupArn"]: t.get("Port")
            for t in call.pages(
                "elbv2", "describe_target_groups", "TargetGroups", "Marker", LoadBalancerArn=arn
            )
        }
        listeners = []
        for listener in raw_listeners:
            arns = [
                a["TargetGroupArn"]
                for a in listener.get("DefaultActions", [])
                if "TargetGroupArn" in a
            ]
            target_ports = sorted({ports[a] for a in arns if ports.get(a) is not None})
            listeners.append(
                ListenerInfo(
                    port=listener["Port"],
                    protocol=listener["Protocol"],
                    target_ports=target_ports,
                    has_certificate=bool(listener.get("Certificates")),
                    ssl_policy=listener.get("SslPolicy"),
                    redirects_to_https=any(
                        a.get("Type") == "redirect"
                        and (a.get("RedirectConfig") or {}).get("Protocol") == "HTTPS"
                        for a in listener.get("DefaultActions", [])
                    ),
                )
            )
        balancers.append(
            LoadBalancerInfo(
                name=lb["LoadBalancerName"],
                type=lb.get("Type", ""),
                scheme=lb.get("Scheme", ""),
                listeners=listeners,
                dns_name=lb.get("DNSName"),
            )
        )

    distributions, cloudfront_status = _read_cloudfront(call, clients, balancers)

    roles = [_role_policy(call, arn) for arn in role_arns]

    summary, cloudtrail_error = _read_cloudtrail(
        call,
        end=now or datetime.now(UTC),
        days=cloudtrail_days,
        max_events=cloudtrail_max_events,
    )

    return AwsInfraScan(
        cluster=cluster,
        vpcs=vpcs,
        subnets=subnets,
        route_tables=route_tables,
        security_groups=groups,
        load_balancers=balancers,
        role_policies=roles,
        cloudtrail=summary,
        duration_seconds=clock() - started,
        cloudfront=distributions,
        cloudfront_status=cloudfront_status,
        cloudtrail_status="NOT_COLLECTED" if cloudtrail_error else "COLLECTED",
        cloudtrail_error=cloudtrail_error,
    )
