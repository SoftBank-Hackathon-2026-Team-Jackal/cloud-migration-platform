"""Ticket 47 (#182): the target connection role covers Terraform and the AWS workloads (B-04·B-14)
and refuses privilege escalation (B-18), evaluated offline.

The evaluator below implements the IAM rules the template relies on (explicit deny wins; an allow
statement matches action, resource and every condition; a missing condition key fails every
operator except `Null`). `infra/connections/simulate-target-role.py` asks IAM the same questions
with `simulate-custom-policy` (read-only); its last result is in the ticket.
"""

import fnmatch
import re
from pathlib import Path

import pytest
import yaml
from cmp_providers.aws_target_access import (
    REVIEWED_MANAGED_POLICIES,
    TERRAFORM_CALLS,
    allowed_cases,
    refusals,
    render_target_policies,
    render_target_policy,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "connections" / "target-role.yaml"
ACCOUNT, REGION, NAME = "111122223333", "ap-northeast-2", "cmp-demo"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/cmp-target-ConnectionRole-ABC"


def policy() -> dict:
    return render_target_policy(TEMPLATE.read_text(), account=ACCOUNT, role_arn=ROLE_ARN)


def _list(value):
    return value if isinstance(value, list) else [value]


def _condition_ok(conditions: dict, context: dict) -> bool:
    for operator, pairs in conditions.items():
        for key, wanted in pairs.items():
            wanted = [str(v) for v in _list(wanted)]
            if operator == "Null":
                if (key not in context) != (wanted == ["true"]):
                    return False
                continue
            if key not in context:
                return False
            actual = context[key]
            if operator in ("StringEquals", "ArnEquals"):
                ok = actual in wanted
            elif operator in ("StringLike", "ArnLike"):
                ok = any(fnmatch.fnmatchcase(actual, pattern) for pattern in wanted)
            else:
                raise AssertionError(f"evaluator does not implement {operator}")
            if not ok:
                return False
    return True


def _matches(statement: dict, action: str, resource: str, context: dict) -> bool:
    actions = [a.lower() for a in _list(statement["Action"])]
    if not any(fnmatch.fnmatchcase(action.lower(), pattern) for pattern in actions):
        return False
    resources = _list(statement["Resource"])
    if resource != "*" and not any(fnmatch.fnmatchcase(resource, r) for r in resources):
        return False
    if resource == "*" and "*" not in resources:
        return False
    return _condition_ok(statement.get("Condition", {}), context)


def decide(document: dict, action: str, resource: str, context: dict) -> str:
    statements = document["Statement"]
    if any(s["Effect"] == "Deny" and _matches(s, action, resource, context) for s in statements):
        return "explicitDeny"
    if any(s["Effect"] == "Allow" and _matches(s, action, resource, context) for s in statements):
        return "allowed"
    return "implicitDeny"


CASES = allowed_cases(ACCOUNT, REGION, NAME)


@pytest.mark.parametrize("case", CASES, ids=[c.label for c in CASES])
def test_terraform_and_workload_calls_are_allowed(case):
    assert decide(policy(), case.action, case.resource, case.context) == "allowed"


REFUSALS = refusals(ACCOUNT, REGION, NAME)


@pytest.mark.parametrize("refusal", REFUSALS, ids=[r.why for r in REFUSALS])
def test_privilege_escalation_is_refused(refusal):
    assert decide(policy(), refusal.action, refusal.resource, refusal.context) != "allowed"


def test_parent_resources_without_the_tag_are_refused_even_if_ec2_sent_request_tags():
    """Lenient reading: a parent VPC also gets aws:RequestTag. It still needs its own tag."""
    vpc = f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc/vpc-0user"
    request = {"aws:RequestTag/cmp:managed-by": "terraform"}
    for action in ("ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup",
                   "ec2:CreateNatGateway"):  # fmt: skip
        assert decide(policy(), action, vpc, request) != "allowed", action


def test_every_terraform_resource_type_has_reviewed_calls():
    types = set()
    for path in (ROOT / "infra" / "terraform" / "aws").glob("*.tf"):
        types |= set(re.findall(r'^resource "([a-z0-9_]+)"', path.read_text(), re.MULTILINE))
    assert types == set(TERRAFORM_CALLS)


def test_terraform_attaches_only_the_reviewed_managed_policies():
    attached = set()
    for path in (ROOT / "infra" / "terraform" / "aws").glob("*.tf"):
        attached |= set(re.findall(r'policy_arn\s*=\s*"(arn:aws:iam::aws:policy/[^"]+)"',
                                   path.read_text()))  # fmt: skip
    assert attached == set(REVIEWED_MANAGED_POLICIES)


def test_template_has_no_inline_role_policy_write_and_fixes_the_prefix():
    template = yaml.safe_load(TEMPLATE.read_text())
    assert template["Parameters"]["ResourcePrefix"]["AllowedValues"] == ["cmp-"]
    allowed = [
        a for s in policy()["Statement"] if s["Effect"] == "Allow" for a in _list(s["Action"])
    ]
    assert "iam:PutRolePolicy" not in allowed
    assert not any(a.startswith(("iam:Get", "iam:List")) and "*" in a for a in allowed)


def test_each_managed_policy_fits_the_iam_size_limit():
    """IAM counts managed policy size without whitespace: 6,144 characters."""
    import json

    documents = render_target_policies(TEMPLATE.read_text(), account=ACCOUNT, role_arn=ROLE_ARN)
    assert len(documents) == 2
    for document in documents:
        assert len(json.dumps(document, separators=(",", ":"))) <= 6144
