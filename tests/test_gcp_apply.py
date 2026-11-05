"""Ticket 10 seam: approved GCP execution request -> saved-Plan Apply -> GKE state/refs.

Terraform, gcloud, AWS STS and the ADC identity lookup are fakes; no cloud call is made.
"""

import json
from pathlib import Path

from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.plan import CloudIdentity, PlanManifest, Preview
from cmp_contracts.provisioning import PlanRef, VersionRef
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes
from cmp_providers.aws_apply import CommandResult, template_evidence
from cmp_providers.gcp_apply import GCP_TEMPLATE_ROOT, apply_gcp_infrastructure
from cmp_providers.gcp_preview import GcpPlanInput
from cmp_providers.plans import manifest_digest

PROJECT = "sbh26-prod"
STATE_ACCOUNT = "714066696240"
ADC = "kms.dev.data@gmail.com"
PLAN_BYTES = b"saved gcp terraform plan (fake)"


def spec(**override):
    data = {
        "migration_id": "migration-001",
        "plan": {"plan_id": "plan-gcp", "revision": 1, "template_version": "gcp-gke-v3"},
        "environment_id": "target-gcp",
        "project_id": PROJECT,
        "region": "asia-northeast3",
        "credential_ref": {"id": "cmp-gcp-prod", "version": "1"},
        "state_ref": {"id": "state-target-gcp", "version": "1"},
        "state_bucket": "cmp-demo-714066696240-apne2-state",
        "state_key": "cmp/gcp/target-gcp/terraform.tfstate",
        "state_region": "ap-northeast-2",
        "state_account_id": STATE_ACCOUNT,
        "cluster_name": "cmp-target-gke",
        "subnet_cidr": "10.50.0.0/20",
        "pods_cidr": "10.52.0.0/14",
        "services_cidr": "10.56.0.0/20",
        "master_cidr": "172.16.0.32/28",
        "api_access_cidrs": ["203.0.113.10/32"],
        "release_channel": "REGULAR",
        "node_machine_type": "e2-medium",
        "node_count": 1,
        "node_disk_gib": 30,
        "node_service_account_email": "cmp-gke-nodes@sbh26-prod.iam.gserviceaccount.com",
    }
    data.update(override)
    return GcpPlanInput.model_validate(data)


def ref(name, kind, content):
    return ArtifactRef(
        id=f"plan-gcp-r1-{name}",
        version="1",
        kind=kind,
        sha256=sha256_bytes(content),
        size_bytes=len(content),
    )


def artifacts(tmp_path, request=None, *, state_ref=None, mode="S3", project=None):
    request = request or spec()
    spec_bytes = canonical_json_bytes(request.model_dump(mode="json"))
    manifest = PlanManifest(
        contract_version="1",
        digest_format="cmp-json-v1",
        migration_id=request.migration_id,
        plan=PlanRef(plan_id="plan-gcp", revision=1, template_version="gcp-gke-v3"),
        target=CloudIdentity(
            provider="GCP",
            environment_id=request.environment_id,
            account_id=project or request.project_id,
            region=request.region,
            state_ref=state_ref or request.state_ref,
        ),
        scope="INFRASTRUCTURE",
        artifacts={
            "spec": ref("spec", "SPEC", spec_bytes),
            "terraform_plan": ref("terraform-plan", "TERRAFORM_PLAN", PLAN_BYTES),
        },
    )
    preview = Preview(
        contract_version="1",
        manifest_digest=manifest_digest(manifest),
        changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "GKE 클러스터"}],
    )
    directory = tmp_path / "plan-gcp-r1"
    directory.mkdir(mode=0o700)
    (directory / "spec.json").write_bytes(spec_bytes)
    (directory / "saved.tfplan").write_bytes(PLAN_BYTES)
    evidence = {**template_evidence(GCP_TEMPLATE_ROOT), "plan_mode": mode}
    (directory / "evidence.json").write_bytes(canonical_json_bytes(evidence))
    return manifest, preview, directory


OUTPUTS = {
    "project_id": {"sensitive": False, "value": PROJECT},
    "network_name": {"sensitive": False, "value": "cmp-target-gke"},
    "subnetwork_name": {"sensitive": False, "value": "cmp-target-gke"},
    "cluster_name": {"sensitive": False, "value": "cmp-target-gke"},
    "cluster_location": {"sensitive": False, "value": "asia-northeast3"},
    "node_pool_name": {"sensitive": False, "value": "cmp-target-gke-primary"},
    "cluster_ca_certificate": {"sensitive": True, "value": "SHOULD-NOT-LEAK"},
}


class FakeRunner:
    def __init__(
        self, *, state_account=STATE_ACCOUNT, apply_rc=0, apply_stderr=b"", status="RUNNING"
    ):
        self.commands = []
        self.state_account = state_account
        self.apply_rc = apply_rc
        self.apply_stderr = apply_stderr
        self.status = status
        self.env_at_init = None

    def __call__(self, args, cwd, env, timeout):
        self.commands.append(list(args))
        tool, *rest = args
        if tool == "aws" and rest[:2] == ["sts", "get-caller-identity"]:
            return CommandResult(0, json.dumps({"Account": self.state_account}).encode(), b"")
        if tool == "gcloud" and rest[:3] == ["container", "clusters", "describe"]:
            return CommandResult(0, json.dumps({"status": self.status}).encode(), b"")
        if tool == "gcloud" and rest[:3] == ["container", "node-pools", "describe"]:
            return CommandResult(0, json.dumps({"status": "RUNNING"}).encode(), b"")
        if rest[:1] == ["init"]:
            self.env_at_init = dict(env)
        if rest[:1] == ["apply"]:
            return CommandResult(self.apply_rc, b"", self.apply_stderr)
        if rest[:1] == ["output"]:
            return CommandResult(0, json.dumps(OUTPUTS).encode(), b"")
        if rest[:2] == ["state", "list"]:
            return CommandResult(0, b"google_compute_network.target\n", b"")
        return CommandResult(0, b"", b"")


def run(manifest, preview, directory, runner, adc=ADC):
    return apply_gcp_infrastructure(
        manifest,
        preview,
        directory,
        aws_profile="cmp-aws-prod",
        gcloud_configuration="cmp-gcp-prod",
        expected_adc_email=ADC,
        terraform_bin="terraform",
        runner=runner,
        adc_email=lambda env: adc,
    )


def terraform_subcommands(runner):
    return [args[1] for args in runner.commands if args[0] == "terraform"]


def test_applies_saved_plan_and_records_gke_refs(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner()
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("SUCCEEDED", "TERRAFORM_APPLIED")
    refs = result["resource_refs"]
    assert refs["gcp_project_id"] == PROJECT
    assert refs["gke_cluster_name"] == "cmp-target-gke"
    assert refs["gke_location"] == "asia-northeast3"
    assert refs["vpc_network"] == "cmp-target-gke"
    assert refs["gke_cluster_status"] == "RUNNING"
    assert refs["node_pool_status"] == "RUNNING"
    assert "SHOULD-NOT-LEAK" not in json.dumps(result)
    apply = next(args for args in runner.commands if args[:2] == ["terraform", "apply"])
    assert apply[-1] == "saved.tfplan"
    assert "plan" not in terraform_subcommands(runner)
    assert "destroy" not in terraform_subcommands(runner)
    init = next(args for args in runner.commands if args[:2] == ["terraform", "init"])
    assert "-backend-config=key=cmp/gcp/target-gcp/terraform.tfstate" in init
    assert "-backend-config=region=ap-northeast-2" in init
    assert runner.env_at_init["AWS_PROFILE"] == "cmp-aws-prod"
    assert "GOOGLE_OAUTH_ACCESS_TOKEN" not in runner.env_at_init
    describe = next(
        a for a in runner.commands if a[:4] == ["gcloud", "container", "clusters", "describe"]
    )
    assert "--project" in describe and PROJECT in describe


def test_changed_plan_bytes_never_reach_terraform(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    (directory / "saved.tfplan").write_bytes(b"tampered")
    runner = FakeRunner()
    assert run(manifest, preview, directory, runner)["code"] == "ARTIFACT_DIGEST_MISMATCH"
    assert runner.commands == []


def test_local_mode_plans_are_never_applied(tmp_path):
    for mode in ("LOCAL_TEST", "LOCAL_PROJECT"):
        base = tmp_path / mode
        base.mkdir()
        manifest, preview, directory = artifacts(base, mode=mode)
        runner = FakeRunner()
        result = run(manifest, preview, directory, runner)
        assert (result["status"], result["code"]) == ("FAILED", "LOCAL_STATE_PLAN")
        assert runner.commands == []


def test_state_identity_must_match_manifest(tmp_path):
    manifest, preview, directory = artifacts(
        tmp_path, state_ref=VersionRef(id="another-state", version="1")
    )
    assert run(manifest, preview, directory, FakeRunner())["code"] == "STATE_MISMATCH"


def test_project_must_match_manifest(tmp_path):
    manifest, preview, directory = artifacts(tmp_path, project="another-project")
    assert run(manifest, preview, directory, FakeRunner())["code"] == "TARGET_MISMATCH"


def test_wrong_adc_principal_blocks_apply(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner()
    result = run(manifest, preview, directory, runner, adc="someone@example.com")
    assert (result["status"], result["code"]) == ("FAILED", "ADC_PRINCIPAL_MISMATCH")
    assert "apply" not in terraform_subcommands(runner)


def test_state_bucket_account_must_match(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner(state_account="111111111111")
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("FAILED", "STATE_ACCOUNT_MISMATCH")
    assert "apply" not in terraform_subcommands(runner)


def test_stale_plan_is_unknown_and_not_reapplied(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner(apply_rc=1, apply_stderr=b"Error: Saved plan is stale")
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("UNKNOWN", "SAVED_PLAN_STALE")
    assert result["resource_refs"]["state_resource_count"] == "1"
    assert terraform_subcommands(runner).count("apply") == 1


def test_partial_failure_does_not_leak_stderr(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner(apply_rc=1, apply_stderr=b"Error: quota token=SECRET123")
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("FAILED", "TERRAFORM_APPLY_FAILED")
    assert "SECRET123" not in json.dumps(result)


def test_cluster_not_running_is_failed(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    result = run(manifest, preview, directory, FakeRunner(status="PROVISIONING"))
    assert (result["status"], result["code"]) == ("FAILED", "CLUSTER_NOT_READY")


def test_template_drift_blocks_apply(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    (directory / "evidence.json").write_bytes(
        canonical_json_bytes({"template_digest": "0" * 64, "plan_mode": "S3"})
    )
    runner = FakeRunner()
    assert run(manifest, preview, directory, runner)["code"] == "TEMPLATE_DRIFT"
    assert runner.commands == []


def test_workspace_is_removed(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    run(manifest, preview, directory, FakeRunner())
    assert not any(p.name.startswith("gcp-apply-") for p in Path(directory.parent).iterdir())
