"""GCP saved Plan boundary with local tool substitutes and no cloud calls."""

import json
import os
import shutil
from pathlib import Path

import pytest
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.artifacts import verify_artifact_bytes
from cmp_providers.gcp_preview import (
    GcpPlanInput,
    plan_gcp_infrastructure,
    preview_from_plan,
    terraform_variables,
)
from cmp_providers.plans import approval_issues, manifest_digest


def request() -> GcpPlanInput:
    return GcpPlanInput.model_validate(
        {
            "migration_id": "migration-001",
            "plan": {"plan_id": "plan-001", "revision": 1, "template_version": "gcp-gke-v1"},
            "environment_id": "demo-001",
            "project_id": "sbh26-test",
            "region": "asia-northeast3",
            "credential_ref": {"id": "credential-001", "version": "1"},
            "state_ref": {"id": "state-001", "version": "1"},
            "state_bucket": "existing-state-bucket",
            "state_key": "cmp/gcp/demo-001/terraform.tfstate",
            "state_region": "ap-northeast-2",
            "state_account_id": "123456789012",
            "cluster_name": "cmp-demo",
            "subnet_cidr": "10.30.0.0/20",
            "pods_cidr": "10.32.0.0/14",
            "services_cidr": "10.36.0.0/20",
            "master_cidr": "172.16.0.0/28",
            "api_access_cidrs": ["203.0.113.10/32"],
            "release_channel": "REGULAR",
            "node_machine_type": "e2-standard-2",
            "node_count": 2,
            "node_disk_gib": 60,
            "node_service_account_email": "cmp-node@sbh26-test.iam.gserviceaccount.com",
        }
    )


def fake_tools(tmp_path: Path, monkeypatch, *, project="sbh26-test", state_account="123456789012"):
    tools = tmp_path / "tools"
    tools.mkdir(parents=True)
    gcloud = tools / "gcloud"
    gcloud.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = config ]; then printf "%s" "{project}"; '
        'else printf "%s" "local-test-token"; fi\n'
    )
    gcloud.chmod(0o755)
    aws = tools / "aws"
    aws.write_text(f"#!/bin/sh\nprintf '%s' '{{\"Account\":\"{state_account}\"}}'\n")
    aws.chmod(0o755)
    resources = [
        "google_compute_network",
        "google_compute_subnetwork",
        "google_compute_router",
        "google_compute_router_nat",
        "google_compute_firewall",
        "google_container_cluster",
        "google_container_node_pool",
    ]
    plan_json = {
        "resource_changes": [
            {
                "mode": "managed",
                "type": kind,
                "change": {
                    "actions": ["create"],
                    "after": {"secret": "TOP_SECRET_NEVER_SHOW"},
                    "after_sensitive": {"secret": True},
                },
            }
            for kind in resources
        ]
    }
    terraform = tools / "terraform"
    terraform.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$TOOL_CALLS"\n'
        'if [ "$1" = plan ]; then printf "binary-PLAN" > saved.tfplan; fi\n'
        f"if [ \"$1\" = show ]; then cat <<'JSON'\n{json.dumps(plan_json)}\nJSON\nfi\n"
    )
    terraform.chmod(0o755)
    template = tmp_path / "template"
    template.mkdir()
    root = Path(__file__).resolve().parents[1] / "infra/terraform/gcp"
    for path in root.glob("*.tf"):
        shutil.copy2(path, template / path.name)
    (template / ".terraform.lock.hcl").write_text("fake provider lock")
    calls = tmp_path / "calls"
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("TOOL_CALLS", str(calls))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"email":"planner@example.com","email_verified":"true"}'

    monkeypatch.setattr(
        "cmp_providers.gcp_preview.urllib.request.urlopen", lambda *_a, **_k: Response()
    )
    return template, terraform, calls


def test_saved_plan_and_redacted_preview(tmp_path, monkeypatch):
    template, terraform, calls = fake_tools(tmp_path, monkeypatch)
    manifest, preview = plan_gcp_infrastructure(
        request(),
        output_dir=tmp_path / "plans",
        gcloud_configuration="cmp-gcp-prod",
        expected_adc_email="planner@example.com",
        aws_profile="cmp-aws-prod",
        terraform_bin=str(terraform),
        template_dir=template,
    )
    stored = tmp_path / "plans/plan-001-r1"
    verify_artifact_bytes(manifest.artifacts["spec"], (stored / "spec.json").read_bytes())
    verify_artifact_bytes(
        manifest.artifacts["terraform_plan"], (stored / "saved.tfplan").read_bytes()
    )
    assert manifest.target.provider == "GCP"
    assert manifest.target.account_id == "sbh26-test"
    assert manifest.scope == "INFRASTRUCTURE"
    assert preview.manifest_digest == manifest_digest(manifest)
    assert not approval_issues(manifest, preview)
    assert {change.name for change in preview.changes} == {
        "VPC Network",
        "Regional subnet",
        "Cloud Router",
        "Cloud NAT",
        "Internal firewall",
        "GKE cluster",
        "GKE node pool",
    }
    shown = " ".join(change.size or "" for change in preview.changes)
    assert "10.32.0.0/14" in shown
    assert "e2-standard-2" in shown
    assert "203.0.113.10/32" in shown
    assert "ap-northeast-2" in calls.read_text()
    assert "apply" not in calls.read_text()
    assert "TOP_SECRET_NEVER_SHOW" not in (stored / "preview.json").read_text()
    assert "TOP_SECRET_NEVER_SHOW" not in (stored / "manifest.json").read_text()
    assert not list((tmp_path / "plans").glob("gcp-plan-*"))
    with pytest.raises(ProvisioningInputError, match="IMMUTABLE_PLAN_EXISTS"):
        plan_gcp_infrastructure(
            request(),
            output_dir=tmp_path / "plans",
            gcloud_configuration="cmp-gcp-prod",
            expected_adc_email="planner@example.com",
            aws_profile="cmp-aws-prod",
            terraform_bin=str(terraform),
            template_dir=template,
        )


def test_local_saved_plan_is_explicitly_unapprovable(tmp_path, monkeypatch):
    template, terraform, calls = fake_tools(tmp_path, monkeypatch)
    payload = request().model_dump()
    payload["project_id"] = "cmp-plan-local"
    payload["node_service_account_email"] = "cmp-node@cmp-plan-local.iam.gserviceaccount.com"
    local_request = GcpPlanInput.model_validate(payload)
    manifest, preview = plan_gcp_infrastructure(
        local_request,
        output_dir=tmp_path / "plans",
        mode="LOCAL_TEST",
        terraform_bin=str(terraform),
        template_dir=template,
    )
    assert {issue.code for issue in preview.errors} == {"LOCAL_STATE_UNAPPROVABLE"}
    assert {issue.code for issue in approval_issues(manifest, preview)} == {"VALIDATION_FAILED"}
    assert (
        json.loads((tmp_path / "plans/plan-001-r1/evidence.json").read_text())["plan_mode"]
        == "LOCAL_TEST"
    )
    assert "-refresh=false" in calls.read_text()
    assert "gcloud" not in calls.read_text()
    assert "aws" not in calls.read_text()
    with pytest.raises(ProvisioningInputError, match="INVALID_TEST_TARGET"):
        plan_gcp_infrastructure(
            request(),
            output_dir=tmp_path / "other",
            mode="LOCAL_TEST",
            terraform_bin=str(terraform),
            template_dir=template,
        )


def test_local_project_checks_adc_then_marks_plan_unapprovable(tmp_path, monkeypatch):
    template, terraform, calls = fake_tools(tmp_path, monkeypatch)
    manifest, preview = plan_gcp_infrastructure(
        request(),
        output_dir=tmp_path / "plans",
        mode="LOCAL_PROJECT",
        gcloud_configuration="cmp-gcp-prod",
        expected_adc_email="planner@example.com",
        terraform_bin=str(terraform),
        template_dir=template,
    )
    assert manifest.target.account_id == "sbh26-test"
    assert [issue.code for issue in preview.errors] == ["LOCAL_STATE_UNAPPROVABLE"]
    assert [issue.code for issue in approval_issues(manifest, preview)] == ["VALIDATION_FAILED"]
    assert (
        json.loads((tmp_path / "plans/plan-001-r1/evidence.json").read_text())["plan_mode"]
        == "LOCAL_PROJECT"
    )
    assert "-refresh=false" in calls.read_text()
    assert "backend-config" not in calls.read_text()


def test_invalid_gcp_topology_rejected_before_cloud(tmp_path):
    payload = request().model_dump()
    payload["pods_cidr"] = "10.30.0.0/21"
    with pytest.raises(ProvisioningInputError, match="CIDR_OVERLAP"):
        terraform_variables(GcpPlanInput.model_validate(payload))
    payload = request().model_dump()
    payload["master_cidr"] = "10.50.0.0/24"
    with pytest.raises(ProvisioningInputError, match="INVALID_TOPOLOGY"):
        terraform_variables(GcpPlanInput.model_validate(payload))
    payload = request().model_dump()
    payload["api_access_cidrs"] = ["0.0.0.0/0"]
    with pytest.raises(ProvisioningInputError, match="PUBLIC_API_EXPOSURE"):
        terraform_variables(GcpPlanInput.model_validate(payload))
    payload = request().model_dump()
    payload["state_key"] = "cmp/gcp/demo-001/r2.tfstate"
    with pytest.raises(ProvisioningInputError, match="INVALID_STATE_KEY"):
        terraform_variables(GcpPlanInput.model_validate(payload))
    payload = request().model_dump()
    payload["node_service_account_email"] = "cmp-node@other-project.iam.gserviceaccount.com"
    with pytest.raises(ProvisioningInputError, match="NODE_SA_PROJECT_MISMATCH"):
        terraform_variables(GcpPlanInput.model_validate(payload))
    assert not (tmp_path / "plans").exists()


def test_wrong_project_state_account_or_adc_never_plans(tmp_path, monkeypatch):
    template, terraform, calls = fake_tools(tmp_path, monkeypatch, project="another-project")
    kwargs = {
        "output_dir": tmp_path / "plans",
        "gcloud_configuration": "cmp-gcp-prod",
        "expected_adc_email": "planner@example.com",
        "aws_profile": "cmp-aws-prod",
        "terraform_bin": str(terraform),
        "template_dir": template,
    }
    with pytest.raises(ProvisioningInputError, match="PROJECT_MISMATCH"):
        plan_gcp_infrastructure(request(), **kwargs)
    assert not calls.exists()
    (tmp_path / "tools/gcloud").write_text(
        '#!/bin/sh\nif [ "$1" = config ]; then printf "%s" "sbh26-test"; '
        'else printf "%s" "local-test-token"; fi\n'
    )
    with pytest.raises(ProvisioningInputError, match="ADC_PRINCIPAL_MISMATCH"):
        plan_gcp_infrastructure(request(), **{**kwargs, "expected_adc_email": "other@example.com"})
    assert not calls.exists()
    (tmp_path / "tools/aws").write_text("#!/bin/sh\nprintf '%s' '{\"Account\":\"999999999999\"}'\n")
    with pytest.raises(ProvisioningInputError, match="STATE_ACCOUNT_MISMATCH"):
        plan_gcp_infrastructure(request(), **kwargs)
    assert not calls.exists()


def test_reject_unreviewed_or_noncreate_plan():
    for kind, actions, code in (
        ("google_sql_database_instance", ["create"], "UNREVIEWED_PLAN"),
        ("google_compute_network", ["delete"], "NON_CREATE_PLAN"),
    ):
        payload = {
            "resource_changes": [{"mode": "managed", "type": kind, "change": {"actions": actions}}]
        }
        with pytest.raises(ProvisioningInputError, match=code):
            preview_from_plan(payload, request(), "a" * 64)
