"""Ticket 23 (#118): CloudFormation connection role templates (structure checks; cfn-lint absent).

Templates use long-form intrinsics only, so `yaml.safe_load` reads them as plain data.
"""

from pathlib import Path

import pytest
import yaml
from cmp_api.connections import TEMPLATES
from cmp_scanners.aws_infra import required_iam_actions

ROOT = Path(__file__).resolve().parents[1] / "infra" / "connections"
READ_PREFIXES = ("Describe", "List", "Get", "Lookup")


def load(name):
    return yaml.safe_load((ROOT / name).read_text())


def statements(template, resource):
    return template["Resources"][resource]["Properties"]["PolicyDocument"]["Statement"]


def actions(statement):
    value = statement["Action"]
    return [value] if isinstance(value, str) else value


@pytest.mark.parametrize("name", sorted(TEMPLATES.values()))
def test_trust_is_the_platform_principal_with_the_external_id_only(name):
    template = load(name)
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert set(template["Parameters"]) >= {"PlatformPrincipalArn", "ExternalId"}
    assert template["Parameters"]["ExternalId"]["AllowedPattern"] == "^cmp-[0-9a-f]{32}$"
    role = template["Resources"]["ConnectionRole"]
    assert role["Type"] == "AWS::IAM::Role"
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    (statement,) = trust
    assert statement["Effect"] == "Allow" and statement["Action"] == "sts:AssumeRole"
    assert statement["Principal"] == {"AWS": {"Ref": "PlatformPrincipalArn"}}
    assert statement["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}
    assert role["Properties"]["MaxSessionDuration"] == 3600
    assert template["Outputs"]["RoleArn"]["Value"] == {"Fn::GetAtt": ["ConnectionRole", "Arn"]}


def test_source_role_grants_exactly_the_scan_reads_plus_cluster_listing():
    template = load("source-role.yaml")
    (statement,) = statements(template, "ReadOnlyScan")
    granted = actions(statement)
    assert statement["Effect"] == "Allow"
    assert sorted(granted) == sorted({*required_iam_actions(), "eks:ListClusters"})
    assert all(action.split(":")[1].startswith(READ_PREFIXES) for action in granted)
    assert not any("*" in action for action in granted)
    assert set(template["Resources"]) == {"ConnectionRole", "ReadOnlyScan"}


def test_target_role_scopes_writes_to_platform_resources():
    """Structure only; ticket 47 evaluates the calls in tests/test_aws_target_access.py."""
    template = load("target-role.yaml")
    by_sid = {
        s["Sid"]: s for name in ("TargetBuild", "TargetCluster") for s in statements(template, name)
    }
    for statement in by_sid.values():
        assert "*" not in actions(statement) and "iam:*" not in actions(statement)
    allowed_wildcards = [
        a for s in by_sid.values() if s["Effect"] == "Allow" for a in actions(s) if "*" in a
    ]
    assert all(a.split(":")[1].startswith(READ_PREFIXES) for a in allowed_wildcards)
    tag = {"Ref": "ManagedByTag"}
    for sid in ("CreateTaggedNetwork", "CreateTaggedInVpc", "CreateTaggedNatGateway"):
        assert by_sid[sid]["Condition"] == {"StringEquals": {"aws:RequestTag/cmp:managed-by": tag}}
    for sid in ("ChangeTagged", "CreateInTaggedParents"):
        assert by_sid[sid]["Condition"] == {"StringEquals": {"aws:ResourceTag/cmp:managed-by": tag}}
    assert "ec2:CreateAction" in by_sid["TagOnCreate"]["Condition"]["StringEquals"]
    iam = by_sid["PlatformIamOnly"]
    assert all(
        "${ResourcePrefix}" in r["Fn::Sub"] or "oidc.eks" in r["Fn::Sub"] for r in iam["Resource"]
    )
    passrole = by_sid["PassPlatformRolesToEks"]
    assert passrole["Condition"]["StringEquals"]["iam:PassedToService"] == [
        "eks.amazonaws.com",
        "ec2.amazonaws.com",
    ]
    denied = by_sid["NeverLongLivedCredentialsOrSelfChange"]
    assert denied["Effect"] == "Deny" and "iam:CreateAccessKey" in actions(denied)
    self_guard = by_sid["NeverChangeThisRole"]
    assert self_guard["Effect"] == "Deny"
    assert self_guard["Resource"] == {"Fn::GetAtt": ["ConnectionRole", "Arn"]}
    # Every non-read Allow carries a condition or a platform-scoped resource list.
    for sid, statement in by_sid.items():
        if statement["Effect"] != "Allow" or sid.startswith("Read") or "Condition" in statement:
            continue
        resources = statement["Resource"]
        assert isinstance(resources, list | dict), sid


def test_eks_view_access_is_one_cluster_scoped_view_entry():
    """Ticket 56: Kubernetes read access for the source role, separate from the role template."""
    template = load("eks-view-access.yaml")
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert set(template["Parameters"]) == {"ClusterName", "PrincipalArn"}
    assert set(template["Resources"]) == {"PlatformViewAccess"}
    entry = template["Resources"]["PlatformViewAccess"]
    assert entry["Type"] == "AWS::EKS::AccessEntry"
    properties = entry["Properties"]
    assert properties["ClusterName"] == {"Ref": "ClusterName"}
    assert properties["PrincipalArn"] == {"Ref": "PrincipalArn"}
    assert properties["Type"] == "STANDARD"
    assert "KubernetesGroups" not in properties and "Username" not in properties
    assert properties["AccessPolicies"] == [
        {
            "PolicyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy",
            "AccessScope": {"Type": "cluster"},
        }
    ]
