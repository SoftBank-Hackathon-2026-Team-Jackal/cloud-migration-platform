"""Ticket 28-A: what stands in front of the app, read from AWS for the security measure.

The scan keeps the load balancer's DNS name and the listener's TLS details (certificate, policy,
redirect to HTTPS), and finds the CloudFront distributions whose origin is one of the cluster
VPC's load balancers (viewer protocol policy, aliases). CloudFront is best effort: a source role
created before this permission existed still scans; the scan says it was denied.

Seam: the scanner with botocore `Stubber` responses, as `test_aws_scan.py`.
"""

import boto3
from botocore.config import Config
from botocore.stub import Stubber
from cmp_scanners.aws_infra import ALLOWED_OPERATIONS, AwsClients, required_iam_actions
from test_aws_scan import (
    LB_DNS,
    REGION,
    activate,
    build_clients,
    client,
    scan,
    stub_happy,
)

OTHER_DNS = f"other-9.{REGION}.elb.amazonaws.com"
REDIRECT = {
    "Port": 80,
    "Protocol": "HTTP",
    "DefaultActions": [
        {
            "Type": "redirect",
            "RedirectConfig": {"Protocol": "HTTPS", "Port": "443", "StatusCode": "HTTP_301"},
        }
    ],
}


def with_cloudfront():
    clients, stubs = build_clients()
    cloudfront = boto3.client(
        "cloudfront",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(retries={"max_attempts": 1}),
    )
    clients = AwsClients(
        clients.eks, clients.ec2, clients.elbv2, clients.iam, clients.cloudtrail, cloudfront
    )
    stubs["cloudfront"] = Stubber(cloudfront)
    return clients, stubs


def distribution(dist_id, origin, *, default="redirect-to-https", behaviors=(), aliases=()):
    """A complete `DistributionSummary` (the stub validates the service shape)."""

    def behavior(policy):
        return {
            "ViewerProtocolPolicy": policy,
            "TargetOriginId": "o1",
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            "SmoothStreaming": False,
            "Compress": False,
            "LambdaFunctionAssociations": {"Quantity": 0},
            "FunctionAssociations": {"Quantity": 0},
            "FieldLevelEncryptionId": "",
        }

    return {
        "Id": dist_id,
        "ARN": f"arn:aws:cloudfront::123456789012:distribution/{dist_id}",
        "ETag": "E1",
        "Status": "Deployed",
        "LastModifiedTime": "2026-11-01T00:00:00Z",
        "DomainName": f"{dist_id.lower()}.cloudfront.net",
        "Aliases": {"Quantity": len(aliases), **({"Items": list(aliases)} if aliases else {})},
        "Origins": {
            "Quantity": 1,
            "Items": [{"Id": "o1", "DomainName": origin, "OriginPath": ""}],
        },
        "DefaultCacheBehavior": behavior(default),
        "CacheBehaviors": {
            "Quantity": len(behaviors),
            **(
                {
                    "Items": [
                        {**behavior(p), "PathPattern": f"/p{i}/*"} for i, p in enumerate(behaviors)
                    ]
                }
                if behaviors
                else {}
            ),
        },
        "CustomErrorResponses": {"Quantity": 0},
        "Comment": "",
        "PriceClass": "PriceClass_All",
        "Enabled": True,
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "WebACLId": "",
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
        "Staging": False,
    }


def page(items, *, next_marker=None):
    listing = {
        "Marker": "",
        "MaxItems": 100,
        "IsTruncated": next_marker is not None,
        "Quantity": len(items),
        **({"Items": items} if items else {}),
        **({"NextMarker": next_marker} if next_marker else {}),
    }
    return {"DistributionList": listing}


# --- the load balancer's front door -----------------------------------------------------------


def test_the_load_balancer_keeps_its_dns_name_and_the_listener_its_tls_details():
    clients, stubs = build_clients()
    stub_happy(
        stubs,
        listener_extra={
            "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
            "Certificates": [{"CertificateArn": "arn:aws:acm:ap-northeast-2:1:certificate/x"}],
        },
    )
    activate(stubs)
    (lb,) = scan(clients).load_balancers
    assert lb.dns_name == LB_DNS
    (listener,) = lb.listeners
    assert listener.has_certificate is True
    assert listener.ssl_policy == "ELBSecurityPolicy-TLS13-1-2-2021-06"
    assert listener.redirects_to_https is False


def test_a_listener_that_redirects_to_https_is_marked():
    clients, stubs = build_clients()
    stub_happy(stubs, listener_extra=REDIRECT)
    activate(stubs)
    (listener,) = scan(clients).load_balancers[0].listeners
    assert (listener.port, listener.protocol) == (80, "HTTP")
    assert listener.redirects_to_https is True and listener.has_certificate is False
    assert listener.ssl_policy is None


def test_without_a_cloudfront_client_nothing_is_read_and_the_status_says_so():
    clients, stubs = build_clients()
    stub_happy(stubs)
    activate(stubs)
    result = scan(clients)
    assert result.cloudfront == [] and result.cloudfront_status == "NOT_REQUESTED"


# --- CloudFront in front of the load balancer -------------------------------------------------


def test_only_distributions_whose_origin_is_a_load_balancer_of_the_vpc_are_kept():
    clients, stubs = with_cloudfront()
    stub_happy(stubs)
    stubs["cloudfront"].add_response(
        "list_distributions",
        page(
            [
                distribution(
                    "EFRONT",
                    LB_DNS.upper(),
                    default="allow-all",
                    behaviors=["redirect-to-https", "allow-all"],
                    aliases=["chat.example.com"],
                ),
                distribution("EOTHER", OTHER_DNS),
                distribution("EBUCKET", "assets.s3.amazonaws.com"),
            ]
        ),
    )
    activate(stubs)
    result = scan(clients)
    assert result.cloudfront_status == "COLLECTED"
    (found,) = result.cloudfront
    assert found.id == "EFRONT" and found.domain_name == "efront.cloudfront.net"
    assert found.aliases == ["chat.example.com"] and found.enabled is True
    assert found.viewer_protocol_policies == ["allow-all", "redirect-to-https"]
    assert found.origin_domains == [LB_DNS]  # the load balancer's name, as the scan knows it


def test_every_page_of_distributions_is_read():
    clients, stubs = with_cloudfront()
    stub_happy(stubs)
    stubs["cloudfront"].add_response(
        "list_distributions", page([distribution("EONE", OTHER_DNS)], next_marker="m2")
    )
    stubs["cloudfront"].add_response(
        "list_distributions", page([distribution("ETWO", LB_DNS, default="https-only")])
    )
    activate(stubs)
    result = scan(clients)
    assert [d.id for d in result.cloudfront] == ["ETWO"]
    assert result.cloudfront[0].viewer_protocol_policies == ["https-only"]


def test_a_denied_cloudfront_read_does_not_fail_the_scan():
    clients, stubs = with_cloudfront()
    stub_happy(stubs)
    stubs["cloudfront"].add_client_error("list_distributions", service_error_code="AccessDenied")
    activate(stubs)
    result = scan(clients)
    assert result.cloudfront_status == "DENIED" and result.cloudfront == []
    assert result.load_balancers and result.security_groups  # the rest of the scan is intact


def test_cloudfront_is_an_allow_listed_read_only_permission():
    assert "cloudfront:ListDistributions" in required_iam_actions()
    assert any(
        (op.client, op.operation) == ("cloudfront", "ListDistributions")
        for op in ALLOWED_OPERATIONS
    )


def test_the_client_helper_still_builds_the_five_original_clients():
    assert client("eks") is not None  # AwsClients stays constructible with five clients
    five = AwsClients(*(client(s) for s in ("eks", "ec2", "elbv2", "iam", "cloudtrail")))
    assert five.cloudfront is None
