"""로컬 대체 도구로 AWS 저장 Plan 경계를 검증한다."""

import json
import os
from pathlib import Path

import pytest
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.artifacts import verify_artifact_bytes
from cmp_providers.aws_preview import (
    AwsPlanInput,
    plan_aws_infrastructure,
    preview_from_plan,
    terraform_variables,
)
from cmp_providers.plans import approval_issues, manifest_digest

AWS_PLAN_ACTIONS = Path(__file__).parent / "fixtures/terraform/aws-create-plan.json"


def request() -> AwsPlanInput:
    return AwsPlanInput.model_validate(
        {
            "migration_id": "migration-001",
            "plan": {"plan_id": "plan-001", "revision": 1, "template_version": "aws-eks-v2"},
            "environment_id": "demo-001",
            "account_id": "123456789012",
            "region": "ap-northeast-2",
            "credential_ref": {"id": "credential-001", "version": "1"},
            "state_ref": {"id": "state-001", "version": "1"},
            "state_bucket": "existing-state-bucket",
            "state_key": "cmp/demo-001/terraform.tfstate",
            "cluster_name": "cmp-demo",
            "kubernetes_version": "1.33",
            "vpc_cidr": "10.20.0.0/16",
            "subnets": [
                {"cidr": "10.20.1.0/24", "zone": "ap-northeast-2a", "role": "PUBLIC"},
                {"cidr": "10.20.2.0/24", "zone": "ap-northeast-2a", "role": "PRIVATE"},
                {"cidr": "10.20.3.0/24", "zone": "ap-northeast-2c", "role": "PUBLIC"},
                {"cidr": "10.20.4.0/24", "zone": "ap-northeast-2c", "role": "PRIVATE"},
            ],
            "api_access_cidrs": ["203.0.113.10/32"],
            "node_instance_type": "t3.large",
            "node_count": 2,
            "node_disk_gib": 60,
        }
    )


def _fake_tools(tmp_path: Path, monkeypatch, *, account="123456789012", fail_plan=False):
    tools = tmp_path / "tools"
    tools.mkdir(parents=True)
    aws = tools / "aws"
    aws.write_text(f"#!/bin/sh\nprintf '%s' '{{\"Account\":\"{account}\"}}'\n")
    aws.chmod(0o755)
    terraform = tools / "terraform"
    plan_json = {
        "resource_changes": [
            {
                "mode": "managed",
                "type": name,
                "change": {
                    "actions": ["create"],
                    "after": {"password": "TOP_SECRET_NEVER_SHOW"},
                    "after_sensitive": {"password": True},
                },
            }
            for name in (
                "aws_vpc",
                "aws_subnet",
                "aws_route",
                "aws_security_group",
                "aws_eks_cluster",
                "aws_eks_node_group",
            )
        ]
    }
    encoded = json.dumps(plan_json)
    terraform.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$TOOL_CALLS"\n'
        + (
            'if [ "$1" = plan ]; then echo TOP_SECRET_NEVER_SHOW >&2; exit 1; fi\n'
            if fail_plan
            else "if [ \"$1\" = plan ]; then printf 'binary-PLAN' > saved.tfplan; fi\n"
        )
        + f"if [ \"$1\" = show ]; then cat <<'JSON'\n{encoded}\nJSON\nfi\n"
    )
    terraform.chmod(0o755)
    calls = tmp_path / "calls"
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("TOOL_CALLS", str(calls))
    return terraform, calls


def test_saved_plan_and_redacted_preview_agree(tmp_path, monkeypatch):
    terraform, calls = _fake_tools(tmp_path, monkeypatch)
    manifest, preview = plan_aws_infrastructure(
        request(),
        output_dir=tmp_path / "plans",
        terraform_bin=str(terraform),
        aws_profile="fixture",
    )
    stored = tmp_path / "plans/plan-001-r1"
    verify_artifact_bytes(
        manifest.artifacts["terraform_plan"], (stored / "saved.tfplan").read_bytes()
    )
    verify_artifact_bytes(manifest.artifacts["spec"], (stored / "spec.json").read_bytes())
    assert preview.manifest_digest == manifest_digest(manifest)
    assert not approval_issues(manifest, preview)
    assert manifest.scope == "INFRASTRUCTURE"
    assert manifest.plan.template_version == "aws-eks-v2"
    assert {change.resource_type for change in preview.changes} == {"NETWORK", "CLUSTER", "NODE"}
    shown = " ".join(change.size or "" for change in preview.changes)
    assert "10.20.1.0/24" in shown and "t3.large" in shown
    assert "203.0.113.10/32" in shown and "단일 NAT" in shown
    assert {warning.code for warning in preview.warnings} == {"ALB_NOT_DEPLOYED", "SINGLE_NAT_AZ"}
    assert "TOP_SECRET_NEVER_SHOW" not in (stored / "preview.json").read_text()
    assert "TOP_SECRET_NEVER_SHOW" not in (stored / "manifest.json").read_text()
    evidence = json.loads((stored / "evidence.json").read_text())
    assert evidence["manifest_digest"] == preview.manifest_digest
    assert evidence["backend_mode"] == "S3"
    assert len(evidence["template_digest"]) == 64
    assert len(evidence["provider_lock_digest"]) == 64
    assert "apply" not in calls.read_text()
    assert not list((tmp_path / "plans").glob("aws-plan-*"))
    with pytest.raises(ProvisioningInputError, match="IMMUTABLE_PLAN_EXISTS"):
        plan_aws_infrastructure(
            request(),
            output_dir=tmp_path / "plans",
            terraform_bin=str(terraform),
            aws_profile="fixture",
        )


def test_local_validation_plan_cannot_be_approved(tmp_path, monkeypatch):
    terraform, calls = _fake_tools(tmp_path, monkeypatch)
    payload = request().model_dump()
    payload["state_bucket"] = None
    local_request = AwsPlanInput.model_validate(payload)
    manifest, preview = plan_aws_infrastructure(
        local_request,
        output_dir=tmp_path / "plans",
        terraform_bin=str(terraform),
        aws_profile="fixture",
        backend_mode="LOCAL_VALIDATION",
    )
    evidence = json.loads((tmp_path / "plans/plan-001-r1/evidence.json").read_text())
    assert evidence["backend_mode"] == "LOCAL_VALIDATION"
    assert evidence["template_digest"] != evidence["effective_template_digest"]
    assert [issue.code for issue in preview.pending_decisions] == ["LOCAL_STATE_VALIDATION_ONLY"]
    assert {issue.code for issue in approval_issues(manifest, preview)} == {"PENDING_DECISION"}
    assert "-backend=false" in calls.read_text()
    assert "-backend-config" not in calls.read_text()
    assert "apply" not in calls.read_text()
    with pytest.raises(ProvisioningInputError, match="MISSING_STATE_BUCKET"):
        plan_aws_infrastructure(
            local_request,
            output_dir=tmp_path / "other",
            terraform_bin=str(terraform),
            aws_profile="fixture",
        )


def test_network_and_api_validation_precedes_cloud_call(tmp_path):
    payload = request().model_dump()
    payload["subnets"][1]["cidr"] = "10.20.1.0/24"
    with pytest.raises(ProvisioningInputError, match="CIDR_OVERLAP"):
        terraform_variables(AwsPlanInput.model_validate(payload))
    payload = request().model_dump()
    payload["api_access_cidrs"] = ["0.0.0.0/0"]
    with pytest.raises(ProvisioningInputError, match="PUBLIC_API_EXPOSURE"):
        terraform_variables(AwsPlanInput.model_validate(payload))
    payload = request().model_dump()
    payload["plan"]["plan_id"] = "../escape"
    with pytest.raises(ProvisioningInputError, match="INVALID_IDENTIFIER"):
        terraform_variables(AwsPlanInput.model_validate(payload))
    payload = request().model_dump()
    payload["state_key"] = "cmp/demo-001/revision-2.tfstate"
    with pytest.raises(ProvisioningInputError, match="INVALID_STATE_KEY"):
        terraform_variables(AwsPlanInput.model_validate(payload))
    assert not (tmp_path / "plans").exists()


def test_account_mismatch_and_tool_error_do_not_expose_output(tmp_path, monkeypatch):
    terraform, calls = _fake_tools(tmp_path, monkeypatch, account="999999999999")
    with pytest.raises(ProvisioningInputError, match="ACCOUNT_MISMATCH"):
        plan_aws_infrastructure(
            request(),
            output_dir=tmp_path / "plans",
            terraform_bin=str(terraform),
            aws_profile="fixture",
        )
    assert not calls.exists()
    (tmp_path / "tools/terraform").unlink()
    terraform, _ = _fake_tools(tmp_path / "second", monkeypatch, fail_plan=True)
    with pytest.raises(ProvisioningInputError) as error:
        plan_aws_infrastructure(
            request(),
            output_dir=tmp_path / "plans",
            terraform_bin=str(terraform),
            aws_profile="fixture",
        )
    assert "TOP_SECRET_NEVER_SHOW" not in str(error.value)


def test_new_network_preview_rejects_unreviewed_or_destructive_plan():
    for resource_type, action, code in (
        ("aws_db_instance", "create", "UNREVIEWED_PLAN"),
        ("aws_vpc", "delete", "DESTRUCTIVE_PLAN"),
    ):
        payload = {
            "resource_changes": [
                {"mode": "managed", "type": resource_type, "change": {"actions": [action]}}
            ]
        }
        with pytest.raises(ProvisioningInputError, match=code):
            preview_from_plan(payload, request(), "a" * 64)


def test_sanitized_provider_plan_fixture_covers_full_aws_topology():
    payload = json.loads(AWS_PLAN_ACTIONS.read_text())
    preview = preview_from_plan(payload, request(), "a" * 64)
    counts = {change.name: change.quantity for change in preview.changes}
    assert counts["VPC"] == 1
    assert counts["서브넷"] == 4
    assert counts["라우트"] == 2
    assert counts["EKS 클러스터"] == 1
    assert counts["관리형 노드 그룹"] == 1
    assert "DO_NOT_LEAK_CANARY" not in preview.model_dump_json()


def test_controller_and_csi_prerequisites_are_reviewed_resources():
    """Ticket 08 (E6): OIDC, EBS CSI add-on and LB Controller IAM are allowed and summarized."""
    payload = json.loads(AWS_PLAN_ACTIONS.read_text())
    for kind in ("aws_iam_openid_connect_provider", "aws_eks_addon", "aws_iam_policy"):
        payload["resource_changes"].append(
            {"mode": "managed", "type": kind, "change": {"actions": ["create"]}}
        )
    preview = preview_from_plan(payload, request(), "a" * 64)
    names = {change.name for change in preview.changes}
    assert {"IAM OIDC 공급자", "EKS EBS CSI 애드온", "Load Balancer Controller IAM 정책"} <= names
    assert "ALB_NOT_DEPLOYED" in {issue.code for issue in preview.warnings}
