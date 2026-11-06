"""Ticket 47 (#182): user kubectl access to the new EKS (B-10) and the platform egress check on the
cluster API CIDRs (D-10·B-21).

Terraform is checked statically (no provider calls); the Plan boundary uses the fake tools of
`test_aws_preview.py`.
"""

import re
from pathlib import Path

import pytest
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.aws_apply import OUTPUT_REFS
from cmp_providers.aws_preview import (
    AwsPlanInput,
    plan_aws_infrastructure,
    preview_from_plan,
    terraform_variables,
)
from cmp_providers.platform_egress import check_platform_egress
from test_aws_preview import _fake_tools, request

ROOT = Path(__file__).resolve().parents[1]
TF = ROOT / "infra" / "terraform" / "aws"
ADMIN = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_Admin_0123"


def _tf(name: str) -> str:
    return (TF / name).read_text()


def _with(**changes) -> AwsPlanInput:
    return AwsPlanInput.model_validate({**request().model_dump(mode="json"), **changes})


def _codes(error: ProvisioningInputError) -> list[str]:
    return [issue.code for issue in error.issues]


def test_cluster_uses_access_entries_and_keeps_the_creator_admin():
    cluster = re.search(
        r'resource "aws_eks_cluster" "target" \{.*?\n\}', _tf("main.tf"), re.DOTALL
    )[0]
    assert re.search(r'authentication_mode\s*=\s*"API"', cluster)
    assert re.search(r"bootstrap_cluster_creator_admin_permissions\s*=\s*true", cluster)


def test_each_admin_principal_gets_a_tagged_entry_with_cluster_admin_scope():
    access = _tf("access.tf")
    assert "for_each      = toset(var.cluster_admin_principal_arns)" in access
    assert 'type          = "STANDARD"' in access
    assert "tags          = local.tags" in access
    assert '"arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"' in access
    assert re.search(r'access_scope \{\s*type = "cluster"\s*\}', access)
    assert re.search(
        r'variable "cluster_admin_principal_arns" \{\s*type\s*=\s*list\(string\)\s*default\s*=\s*\[\]',
        _tf("variables.tf"),
    )


def test_security_group_rule_is_tagged_for_the_target_role():
    rule = re.search(
        r'resource "aws_vpc_security_group_egress_rule" "cluster" \{.*?\n\}',
        _tf("main.tf"),
        re.DOTALL,
    )[0]
    assert re.search(r"tags\s*=\s*local.tags", rule)


def test_admin_principals_reach_terraform_and_the_handoff_refs():
    variables = terraform_variables(_with(cluster_admin_principal_arns=[ADMIN]))
    assert variables["cluster_admin_principal_arns"] == [ADMIN]
    assert terraform_variables(request())["cluster_admin_principal_arns"] == []
    assert 'output "cluster_admin_principal_arns"' in _tf("outputs.tf")
    assert OUTPUT_REFS["cluster_admin_principal_arns"] == "eks_cluster_admin_principal_arns"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("arn:aws:iam::123456789012:root", "INVALID_PRINCIPAL_ARN"),
        ("arn:aws:sts::123456789012:assumed-role/Admin/me", "INVALID_PRINCIPAL_ARN"),
        (
            "arn:aws:iam::123456789012:role/aws-service-role/eks.amazonaws.com/X",
            "INVALID_PRINCIPAL_ARN",
        ),
        ("arn:aws:iam::123456789012:group/admins", "INVALID_PRINCIPAL_ARN"),
        ("not-an-arn", "INVALID_PRINCIPAL_ARN"),
    ],
)
def test_admin_principal_must_be_an_iam_role_or_user(value, code):
    with pytest.raises(ProvisioningInputError) as error:
        terraform_variables(_with(cluster_admin_principal_arns=[value]))
    assert _codes(error.value) == [code]


def test_duplicate_admin_principals_are_refused():
    with pytest.raises(ProvisioningInputError) as error:
        terraform_variables(_with(cluster_admin_principal_arns=[ADMIN, ADMIN]))
    assert _codes(error.value) == ["DUPLICATE_PRINCIPAL_ARN"]


def test_other_account_principal_is_accepted_as_a_standard_entry():
    other = "arn:aws:iam::210987654321:user/ops"
    assert terraform_variables(_with(cluster_admin_principal_arns=[other]))[
        "cluster_admin_principal_arns"
    ] == [other]


def _plan_json(*types):
    return {
        "resource_changes": [
            {"mode": "managed", "type": t, "change": {"actions": ["create"]}} for t in types
        ]
    }


BASE = ("aws_vpc", "aws_eks_cluster", "aws_eks_node_group")


def test_preview_shows_who_gets_kubectl_and_warns_when_nobody_but_the_platform():
    with_admin = _with(cluster_admin_principal_arns=[ADMIN])
    plan = _plan_json(*BASE, "aws_eks_access_entry", "aws_eks_access_policy_association")
    preview = preview_from_plan(plan, with_admin, "0" * 64)
    entries = [c for c in preview.changes if c.name == "EKS 사용자 접근 항목"]
    assert entries and entries[0].size == f"클러스터 관리자: {ADMIN}"
    assert "NO_USER_CLUSTER_ACCESS" not in [w.code for w in preview.warnings]
    bare = preview_from_plan(_plan_json(*BASE), request(), "0" * 64)
    assert "NO_USER_CLUSTER_ACCESS" in [w.code for w in bare.warnings]


@pytest.mark.parametrize(
    ("api", "egress", "ok"),
    [
        (["13.209.240.49/32"], ["13.209.240.49/32"], True),
        (["13.209.240.0/24", "203.0.113.10/32"], ["13.209.240.49/32"], True),
        (["203.0.113.10/32"], ["13.209.240.49/32"], False),
        (["13.209.240.49/32"], ["13.209.240.49/32", "15.164.0.10/32"], False),
    ],
)
def test_platform_egress_must_be_inside_the_api_cidrs(api, egress, ok):
    if ok:
        check_platform_egress(api, egress, path="api_access_cidrs")
        return
    with pytest.raises(ProvisioningInputError) as error:
        check_platform_egress(api, egress, path="api_access_cidrs")
    assert _codes(error.value) == ["PLATFORM_EGRESS_NOT_ALLOWED"]


def test_invalid_configured_egress_is_a_configuration_error():
    with pytest.raises(ProvisioningInputError) as error:
        check_platform_egress(["13.209.240.49/32"], ["13.209.240.49"], path="api_access_cidrs")
    assert _codes(error.value) == ["INVALID_PLATFORM_EGRESS"]


def test_plan_refuses_api_cidrs_without_the_platform_before_any_command(tmp_path, monkeypatch):
    terraform, calls = _fake_tools(tmp_path, monkeypatch)
    with pytest.raises(ProvisioningInputError) as error:
        plan_aws_infrastructure(
            request(),
            output_dir=tmp_path / "plans",
            terraform_bin=str(terraform),
            aws_profile="fixture",
            platform_egress_cidrs=("13.209.240.49/32",),
        )
    assert _codes(error.value) == ["PLATFORM_EGRESS_NOT_ALLOWED"]
    assert not calls.exists()


def test_plan_without_configured_egress_warns_it_was_not_checked(tmp_path, monkeypatch):
    terraform, _ = _fake_tools(tmp_path, monkeypatch)
    _, preview = plan_aws_infrastructure(
        request(), output_dir=tmp_path / "plans", terraform_bin=str(terraform), aws_profile="f"
    )
    assert "PLATFORM_EGRESS_UNCHECKED" in [w.code for w in preview.warnings]
    terraform, _ = _fake_tools(tmp_path / "b", monkeypatch)
    _, checked = plan_aws_infrastructure(
        request(),
        output_dir=tmp_path / "plans-b",
        terraform_bin=str(terraform),
        aws_profile="f",
        platform_egress_cidrs=("203.0.113.10/32",),
    )
    assert "PLATFORM_EGRESS_UNCHECKED" not in [w.code for w in checked.warnings]


def test_publisher_passes_the_configured_platform_egress(monkeypatch):
    from cmp_worker import plan_publish

    seen = {}
    monkeypatch.setattr(
        plan_publish, "plan_aws_infrastructure", lambda request, **kw: seen.update(kw)
    )
    publisher = plan_publish.PlanPublisher(
        None, None, aws_profile="p", platform_egress_cidrs=("13.209.240.49/32",)
    )
    publisher._planner("AWS")(request())
    assert seen["platform_egress_cidrs"] == ("13.209.240.49/32",)


def test_worker_setting_parses_comma_separated_egress(monkeypatch):
    from cmp_worker.config import Settings

    monkeypatch.setenv("PROVISIONING_PLATFORM_EGRESS_CIDRS", "13.209.240.49/32, 15.164.0.0/30")
    assert Settings().platform_egress_cidrs() == ("13.209.240.49/32", "15.164.0.0/30")
    monkeypatch.delenv("PROVISIONING_PLATFORM_EGRESS_CIDRS")
    assert Settings().platform_egress_cidrs() == ()


def test_template_version_moves_to_v4_with_access_entries():
    from cmp_api.plan_inputs import CLOUD_TEMPLATES

    assert CLOUD_TEMPLATES["AWS"] == "aws-eks-v4"
    with pytest.raises(ProvisioningInputError) as error:
        terraform_variables(
            _with(plan={"plan_id": "plan-001", "revision": 1, "template_version": "aws-eks-v3"})
        )
    assert _codes(error.value) == ["UNSUPPORTED_TEMPLATE"]
