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
    template = load("target-role.yaml")
    by_sid = {s["Sid"]: s for s in statements(template, "TargetBuild")}
    for statement in by_sid.values():
        assert "*" not in actions(statement) and "iam:*" not in actions(statement)
    allowed_wildcards = [
        a for s in by_sid.values() if s["Effect"] == "Allow" for a in actions(s) if "*" in a
    ]
    assert all(a.split(":")[1].startswith(READ_PREFIXES) for a in allowed_wildcards)
    tag = {"Ref": "ManagedByTag"}
    for sid in ("CreateTagged", "TagOnCreate"):
        assert by_sid[sid]["Condition"] == {"StringEquals": {"aws:RequestTag/cmp:managed-by": tag}}
    assert by_sid["ChangeTagged"]["Condition"] == {
        "StringEquals": {"aws:ResourceTag/cmp:managed-by": tag}
    }
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
    # Every non-read Allow carries a condition or a platform-prefixed resource.
    for sid, statement in by_sid.items():
        if statement["Effect"] != "Allow" or sid == "Read":
            continue
        # SecurityGroupRules (46) is scoped to the new rule resource type; the same call must
        # also pass ChangeTagged for the tagged security group itself.
        assert "Condition" in statement or sid in {"PlatformIamOnly", "SecurityGroupRules"}, sid
