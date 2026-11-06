"""Ticket 08 seam: approved AWS execution request -> saved-Plan Apply -> infra state/refs.

Terraform and AWS CLI are replaced by a fake command runner; no cloud call is made.
"""

import json
from pathlib import Path

from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.plan import CloudIdentity, PlanManifest, Preview
from cmp_contracts.provisioning import PlanRef, VersionRef
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes
from cmp_providers.aws_apply import CommandResult, apply_aws_infrastructure, template_evidence
from cmp_providers.aws_preview import AwsPlanInput
from cmp_providers.plans import manifest_digest

ACCOUNT = "714066696240"
PLAN_BYTES = b"saved terraform plan bytes (fake)"
SECRET_LOOKING_STDERR = b"Error: something failed token=abc123SECRET"


def spec(**override):
    data = {
        "migration_id": "migration-001",
        "plan": {"plan_id": "plan-aws", "revision": 1, "template_version": "aws-eks-v4"},
        "environment_id": "target-aws",
        "account_id": ACCOUNT,
        "region": "ap-northeast-2",
        "credential_ref": {"id": "cred-aws", "version": "1"},
        "state_ref": {"id": "state-target-aws", "version": "1"},
        "state_bucket": "cmp-demo-714066696240-apne2-state",
        "state_key": "cmp/target-aws/terraform.tfstate",
        "cluster_name": "cmp-target",
        "kubernetes_version": "1.33",
        "vpc_cidr": "10.40.0.0/16",
        "subnets": [
            {"cidr": "10.40.0.0/19", "zone": "ap-northeast-2a", "role": "PUBLIC"},
            {"cidr": "10.40.32.0/19", "zone": "ap-northeast-2c", "role": "PUBLIC"},
            {"cidr": "10.40.64.0/19", "zone": "ap-northeast-2a", "role": "PRIVATE"},
            {"cidr": "10.40.96.0/19", "zone": "ap-northeast-2c", "role": "PRIVATE"},
        ],
        "api_access_cidrs": ["203.0.113.10/32"],
        "node_instance_type": "t3.medium",
        "node_count": 2,
        "node_disk_gib": 20,
    }
    data.update(override)
    return AwsPlanInput.model_validate(data)


def ref(name, kind, content):
    return ArtifactRef(
        id=f"plan-aws-r1-{name}",
        version="1",
        kind=kind,
        sha256=sha256_bytes(content),
        size_bytes=len(content),
    )


def artifacts(tmp_path, request=None, *, state_ref=None, plan_bytes=PLAN_BYTES):
    request = request or spec()
    spec_bytes = canonical_json_bytes(request.model_dump(mode="json"))
    manifest = PlanManifest(
        contract_version="1",
        digest_format="cmp-json-v1",
        migration_id=request.migration_id,
        plan=PlanRef(plan_id="plan-aws", revision=1, template_version="aws-eks-v4"),
        target=CloudIdentity(
            provider="AWS",
            environment_id=request.environment_id,
            account_id=request.account_id,
            region=request.region,
            state_ref=state_ref or request.state_ref,
        ),
        scope="INFRASTRUCTURE",
        artifacts={
            "spec": ref("spec", "SPEC", spec_bytes),
            "terraform_plan": ref("terraform-plan", "TERRAFORM_PLAN", plan_bytes),
        },
    )
    preview = Preview(
        contract_version="1",
        manifest_digest=manifest_digest(manifest),
        changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "EKS 클러스터"}],
    )
    directory = tmp_path / "plan-aws-r1"
    directory.mkdir(mode=0o700)
    (directory / "spec.json").write_bytes(spec_bytes)
    (directory / "saved.tfplan").write_bytes(plan_bytes)
    (directory / "evidence.json").write_bytes(canonical_json_bytes(template_evidence()))
    return manifest, preview, directory


OUTPUTS = {
    "vpc_id": {"sensitive": False, "value": "vpc-0abc"},
    "cluster_name": {"sensitive": False, "value": "cmp-target"},
    "cluster_arn": {"sensitive": False, "value": "arn:aws:eks:ap-northeast-2:1:cluster/x"},
    "node_group_name": {"sensitive": False, "value": "cmp-target-primary"},
    "oidc_provider_arn": {"sensitive": False, "value": "arn:aws:iam::1:oidc-provider/x"},
    "certificate_authority": {"sensitive": True, "value": "SHOULD-NOT-LEAK"},
}


class FakeRunner:
    def __init__(self, *, account=ACCOUNT, apply_rc=0, apply_stderr=b"", cluster="ACTIVE"):
        self.commands = []
        self.account = account
        self.apply_rc = apply_rc
        self.apply_stderr = apply_stderr
        self.cluster = cluster
        self.files_at_apply = None

    def __call__(self, args, cwd, env, timeout):
        self.commands.append(list(args))
        tool, *rest = args
        if tool == "aws" and rest[:2] == ["sts", "get-caller-identity"]:
            return CommandResult(0, json.dumps({"Account": self.account}).encode(), b"")
        if tool == "aws" and rest[:2] == ["eks", "describe-cluster"]:
            return CommandResult(0, json.dumps({"cluster": {"status": self.cluster}}).encode(), b"")
        if tool == "aws" and rest[:2] == ["eks", "describe-nodegroup"]:
            return CommandResult(0, json.dumps({"nodegroup": {"status": "ACTIVE"}}).encode(), b"")
        if rest[:1] == ["apply"]:
            self.files_at_apply = sorted(p.name for p in Path(cwd).iterdir())
            return CommandResult(self.apply_rc, b"", self.apply_stderr)
        if rest[:1] == ["output"]:
            return CommandResult(0, json.dumps(OUTPUTS).encode(), b"")
        if rest[:2] == ["state", "list"]:
            return CommandResult(0, b'aws_vpc.target\naws_subnet.public["a"]\n', b"")
        return CommandResult(0, b"", b"")  # init


def run(manifest, preview, directory, runner):
    return apply_aws_infrastructure(
        manifest,
        preview,
        directory,
        aws_profile="cmp-aws-prod",
        terraform_bin="terraform",
        runner=runner,
    )


def terraform_subcommands(runner):
    return [args[1] for args in runner.commands if args[0] == "terraform"]


def test_applies_only_the_saved_plan_and_records_refs(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner()
    result = run(manifest, preview, directory, runner)
    assert result["status"] == "SUCCEEDED"
    assert result["code"] == "TERRAFORM_APPLIED"
    assert result["resource_refs"]["eks_cluster_name"] == "cmp-target"
    assert result["resource_refs"]["vpc_id"] == "vpc-0abc"
    assert result["resource_refs"]["eks_cluster_status"] == "ACTIVE"
    assert result["resource_refs"]["node_group_status"] == "ACTIVE"
    assert "SHOULD-NOT-LEAK" not in json.dumps(result)
    apply = next(args for args in runner.commands if args[:2] == ["terraform", "apply"])
    assert apply[-1] == "saved.tfplan"
    assert "plan" not in terraform_subcommands(runner)
    assert "destroy" not in terraform_subcommands(runner)
    init = next(args for args in runner.commands if args[:2] == ["terraform", "init"])
    assert "-backend-config=key=cmp/target-aws/terraform.tfstate" in init
    assert "-backend-config=use_lockfile=true" in init


def test_changed_plan_bytes_never_reach_terraform(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    (directory / "saved.tfplan").write_bytes(b"tampered")
    runner = FakeRunner()
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("FAILED", "ARTIFACT_DIGEST_MISMATCH")
    assert runner.commands == []


def test_missing_artifact_directory_is_failed(tmp_path):
    manifest, preview, _ = artifacts(tmp_path)
    result = run(manifest, preview, tmp_path / "missing", FakeRunner())
    assert (result["status"], result["code"]) == ("FAILED", "ARTIFACT_NOT_FOUND")


def test_other_aws_account_blocks_apply(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner(account="111111111111")
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("FAILED", "ACCOUNT_MISMATCH")
    assert "apply" not in terraform_subcommands(runner)


def test_state_identity_must_match_manifest(tmp_path):
    manifest, preview, directory = artifacts(
        tmp_path, state_ref=VersionRef(id="another-state", version="1")
    )
    runner = FakeRunner()
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("FAILED", "STATE_MISMATCH")
    assert runner.commands == []


def test_local_validation_plan_is_never_applied(tmp_path):
    manifest, preview, directory = artifacts(tmp_path, spec(state_bucket=None))
    runner = FakeRunner()
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("FAILED", "LOCAL_STATE_PLAN")
    assert runner.commands == []


def test_stale_saved_plan_is_unknown_and_inspected_not_reapplied(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner(apply_rc=1, apply_stderr=b"Error: Saved plan is stale")
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("UNKNOWN", "SAVED_PLAN_STALE")
    assert result["resource_refs"]["state_resource_count"] == "2"
    assert terraform_subcommands(runner).count("apply") == 1


def test_partial_apply_failure_is_recorded_without_leaking_stderr(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner(apply_rc=1, apply_stderr=SECRET_LOOKING_STDERR)
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("FAILED", "TERRAFORM_APPLY_FAILED")
    assert result["resource_refs"]["state_resource_count"] == "2"
    assert "abc123SECRET" not in json.dumps(result)
    assert "destroy" not in terraform_subcommands(runner)


def test_cluster_not_active_after_apply_is_failed(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    result = run(manifest, preview, directory, FakeRunner(cluster="CREATING"))
    assert (result["status"], result["code"]) == ("FAILED", "CLUSTER_NOT_READY")


def test_non_aws_or_workload_scope_is_rejected(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    data = manifest.model_dump(mode="json")
    data["scope"] = "WORKLOADS"
    workloads = PlanManifest.model_validate(data)
    result = run(workloads, preview, directory, FakeRunner())
    assert (result["status"], result["code"]) == ("FAILED", "UNSUPPORTED_EXECUTION_SCOPE")


def test_workspace_holds_template_and_plan_only_and_is_removed(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = FakeRunner()
    run(manifest, preview, directory, runner)
    assert "saved.tfplan" in runner.files_at_apply
    assert "main.tf" in runner.files_at_apply
    assert ".terraform.lock.hcl" in runner.files_at_apply
    assert not any(p.name.startswith("aws-apply-") for p in directory.parent.iterdir())


def test_template_drift_since_plan_blocks_apply(tmp_path):
    """The Apply re-creates the workspace from the reviewed template; drift must not slip in."""
    manifest, preview, directory = artifacts(tmp_path)
    (directory / "evidence.json").write_bytes(
        canonical_json_bytes({"template_digest": "0" * 64, "provider_lock_digest": "0" * 64})
    )
    runner = FakeRunner()
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("FAILED", "TEMPLATE_DRIFT")
    assert "apply" not in terraform_subcommands(runner)


class InterruptedRunner(FakeRunner):
    """Ticket 53: Apply stopped at the deadline; Terraform left `errored.tfstate` behind."""

    def __init__(self, *, stopped="TERMINATED", lock=b"", lock_rc=254, **kwargs):
        super().__init__(**kwargs)
        self.stopped, self.lock, self.lock_rc = stopped, lock, lock_rc

    def __call__(self, args, cwd, env, timeout):
        if args[:3] == ["aws", "s3api", "head-object"]:
            self.commands.append(list(args))
            stderr = b"" if self.lock_rc == 0 else b"An error occurred (404): Not Found"
            return CommandResult(self.lock_rc, self.lock, stderr)
        if args[1:2] == ["apply"]:
            self.commands.append(list(args))
            (Path(cwd) / "errored.tfstate").write_bytes(b'{"version": 4, "serial": 9}')
            return CommandResult(-1, b"", b"", self.stopped)
        return super().__call__(args, cwd, env, timeout)


def test_interrupted_apply_reports_stop_lock_and_preserves_errored_state(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    runner = InterruptedRunner(stopped="KILLED", lock_rc=0, lock=b'{"ContentLength": 1}')
    result = run(manifest, preview, directory, runner)
    assert (result["status"], result["code"]) == ("UNKNOWN", "TERRAFORM_APPLY_INTERRUPTED")
    refs = result["resource_refs"]
    assert refs["stopped"] == "KILLED"
    assert refs["state_lock"] == "HELD"
    assert refs["state_recovery"] == "PRESERVED"
    kept = list((tmp_path / "state-recovery").iterdir())
    assert len(kept) == 1 and kept[0].name.startswith("plan-aws-r1-")
    assert kept[0].read_bytes() == b'{"version": 4, "serial": 9}'
    lock = next(c for c in runner.commands if c[:3] == ["aws", "s3api", "head-object"])
    assert lock[lock.index("--profile") + 1] == "cmp-aws-prod"  # State (platform) profile
    assert "cmp/target-aws/terraform.tfstate.tflock" in lock


def test_graceful_stop_with_released_lock_is_reported_as_absent(tmp_path):
    manifest, preview, directory = artifacts(tmp_path)
    result = run(manifest, preview, directory, InterruptedRunner())
    refs = result["resource_refs"]
    assert (refs["stopped"], refs["state_lock"]) == ("TERMINATED", "ABSENT")
