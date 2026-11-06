"""Ticket 53 (D5~D7, D10) seam: executed INFRASTRUCTURE Plan → reviewed Terraform destroy.

Terraform, AWS CLI and the ADC lookup are fakes; no cloud call is made.
"""

import json
from pathlib import Path

import pytest
import test_aws_apply as aws_case
import test_gcp_apply as gcp_case
from cmp_providers.aws_apply import CommandResult
from cmp_providers.cloud_destroy import apply_destroy, plan_destroy

AWS_ADDRESSES = ["aws_eks_cluster.target", "aws_subnet.public[0]", "aws_vpc.target"]
GCP_CLUSTER = "google_container_cluster.target"


def destroy_plan(addresses, *, actions=("delete",), prior=None):
    return {
        "resource_changes": [
            {"address": a, "type": a.split(".")[0], "change": {"actions": list(actions)}}
            for a in addresses
        ],
        "prior_state": {"values": {"root_module": {"resources": prior or []}}},
    }


def unprotect_plan(changes=None):
    before = {"name": "cmp-target", "deletion_protection": True, "node_count": 1}
    after = dict(before, deletion_protection=False)
    return {
        "resource_changes": changes
        or [
            {
                "address": GCP_CLUSTER,
                "type": "google_container_cluster",
                "change": {"actions": ["update"], "before": before, "after": after},
            },
            {"address": "google_compute_network.target", "change": {"actions": ["no-op"]}},
        ]
    }


class DestroyRunner:
    """Fake terraform/aws: `show -json <file>` returns the plan registered for that file."""

    def __init__(self, *, account=aws_case.ACCOUNT, plans=None, apply_rc=None, remaining=b""):
        self.commands, self.envs = [], []
        self.account = account
        self.plans = plans or {}
        self.destroy_plans = list(self.plans.pop("destroy_sequence", []))
        self.apply_rc = apply_rc or {}
        self.remaining = remaining
        self.var_files = []

    def __call__(self, args, cwd, env, timeout):
        self.commands.append(list(args))
        self.envs.append(dict(env))
        tool, *rest = args
        if tool == "aws" and rest[:2] == ["sts", "get-caller-identity"]:
            return CommandResult(0, json.dumps({"Account": self.account}).encode(), b"")
        if tool == "aws" and rest[:2] == ["s3api", "head-object"]:
            return CommandResult(254, b"", b"(404) Not Found")
        if rest[:1] == ["plan"]:
            self.var_files.append(json.loads((Path(cwd) / "input.auto.tfvars.json").read_text()))
            return CommandResult(0, b"", b"")
        if rest[:2] == ["show", "-json"]:
            name = Path(rest[2]).name
            if name == "destroy.tfplan":
                plan = (
                    self.destroy_plans.pop(0)
                    if len(self.destroy_plans) > 1
                    else (self.destroy_plans[0])
                )
                return CommandResult(0, json.dumps(plan).encode(), b"")
            return CommandResult(0, json.dumps(self.plans[name]).encode(), b"")
        if rest[:1] == ["apply"]:
            name = Path(rest[-1]).name
            code = self.apply_rc.get(name, 0)
            if code == -1:
                (Path(cwd) / "errored.tfstate").write_bytes(b"{}")
                return CommandResult(-1, b"", b"", "TERMINATED")
            return CommandResult(code, b"", b"secret=abc" if code else b"")
        if rest[:2] == ["state", "list"]:
            return CommandResult(0, self.remaining, b"")
        return CommandResult(0, b"", b"")  # init

    def terraform(self):
        return [a[1:] for a in self.commands if a[0] == "terraform"]


def aws_plan(tmp_path, runner, **kwargs):
    manifest, _, directory = aws_case.artifacts(tmp_path, **kwargs)
    return (
        manifest,
        directory,
        plan_destroy(
            manifest,
            directory,
            aws_profile="cmp-aws-prod",
            terraform_bin="terraform",
            runner=runner,
        ),
    )


def aws_apply(manifest, directory, runner, digest):
    return apply_destroy(
        manifest, directory, review_digest=digest, aws_profile="cmp-aws-prod",
        terraform_bin="terraform", runner=runner,
    )  # fmt: skip


def test_aws_destroy_plan_lists_only_deletes_and_never_applies(tmp_path):
    runner = DestroyRunner(plans={"destroy_sequence": [destroy_plan(AWS_ADDRESSES)]})
    _, _, result = aws_plan(tmp_path, runner)
    assert (result["status"], result["code"]) == ("SUCCEEDED", "DESTROY_PLANNED")
    assert result["review"]["addresses"] == sorted(AWS_ADDRESSES)
    assert result["review"]["unprotect"] is False
    assert len(result["review"]["digest"]) == 64
    assert result["resource_refs"]["delete_count"] == "3"
    plan = next(c for c in runner.terraform() if c[0] == "plan")
    assert "-destroy" in plan and "-out=destroy.tfplan" in plan
    assert not any(c[0] == "apply" for c in runner.terraform())
    assert runner.var_files[0]["name"] == "cmp-target"  # variables rebuilt from the Spec


def test_destroy_plan_with_any_non_delete_change_is_unsafe(tmp_path):
    plan = destroy_plan(AWS_ADDRESSES, actions=("create",))
    _, _, result = aws_plan(tmp_path, DestroyRunner(plans={"destroy_sequence": [plan]}))
    assert (result["status"], result["code"]) == ("FAILED", "DESTROY_PLAN_UNSAFE")


def test_empty_state_has_nothing_to_destroy(tmp_path):
    _, _, result = aws_plan(tmp_path, DestroyRunner(plans={"destroy_sequence": [destroy_plan([])]}))
    assert (result["status"], result["code"]) == ("SUCCEEDED", "NOTHING_TO_DESTROY")


def test_destroy_runs_as_the_connection_and_keeps_state_on_the_platform_profile(tmp_path):
    """D7 (46): provider = connection profile, S3 backend = platform profile."""
    runner = DestroyRunner(plans={"destroy_sequence": [destroy_plan(AWS_ADDRESSES)]})
    request = aws_case.spec(credential_ref={"id": "conn-0123456789ab", "version": "1"})
    aws_plan(tmp_path, runner, request=request)
    init = next(c for c in runner.terraform() if c[0] == "init")
    assert "-backend-config=profile=cmp-aws-prod" in init
    sts = next(c for c in runner.commands if c[:3] == ["aws", "sts", "get-caller-identity"])
    assert sts[sts.index("--profile") + 1] == "cmp-conn-0123456789ab"
    plan_env = runner.envs[[c[1:2] for c in runner.commands].index(["plan"])]
    assert plan_env["AWS_PROFILE"] == "cmp-conn-0123456789ab"


def test_other_account_blocks_destroy_plan(tmp_path):
    runner = DestroyRunner(account="111111111111", plans={"destroy_sequence": [destroy_plan([])]})
    _, _, result = aws_plan(tmp_path, runner)
    assert (result["status"], result["code"]) == ("FAILED", "ACCOUNT_MISMATCH")
    assert not runner.terraform()


def test_changed_spec_bytes_block_destroy(tmp_path):
    manifest, _, directory = aws_case.artifacts(tmp_path)
    (directory / "spec.json").write_bytes(b"{}")
    runner = DestroyRunner(plans={"destroy_sequence": [destroy_plan([])]})
    result = plan_destroy(manifest, directory, aws_profile="cmp-aws-prod", runner=runner)
    assert (result["status"], result["code"]) == ("FAILED", "ARTIFACT_DIGEST_MISMATCH")


def test_confirmed_destroy_replans_and_applies_only_the_reviewed_addresses(tmp_path):
    runner = DestroyRunner(plans={"destroy_sequence": [destroy_plan(AWS_ADDRESSES)]})
    manifest, directory, planned = aws_plan(tmp_path, runner)
    result = aws_apply(manifest, directory, runner, planned["review"]["digest"])
    assert (result["status"], result["code"]) == ("SUCCEEDED", "DESTROYED")
    assert result["resource_refs"]["destroyed_count"] == "3"
    applied = [c for c in runner.terraform() if c[0] == "apply"]
    assert len(applied) == 1 and applied[0][-1] == "destroy.tfplan"
    assert "-lock=true" in applied[0]


def test_wrong_review_digest_never_applies(tmp_path):
    runner = DestroyRunner(plans={"destroy_sequence": [destroy_plan(AWS_ADDRESSES)]})
    manifest, directory, _ = aws_plan(tmp_path, runner)
    result = aws_apply(manifest, directory, runner, "0" * 64)
    assert (result["status"], result["code"]) == ("FAILED", "DESTROY_REVIEW_MISMATCH")
    assert not any(c[0] == "apply" for c in runner.terraform())


def test_state_that_changed_since_review_is_not_destroyed(tmp_path):
    reviewed = destroy_plan(AWS_ADDRESSES)
    runner = DestroyRunner(plans={"destroy_sequence": [reviewed]})
    manifest, directory, planned = aws_plan(tmp_path, runner)
    runner.destroy_plans = [destroy_plan([*AWS_ADDRESSES, "aws_eip.new"])]
    result = aws_apply(manifest, directory, runner, planned["review"]["digest"])
    assert (result["status"], result["code"]) == ("FAILED", "DESTROY_REVIEW_MISMATCH")


def test_failed_destroy_is_recorded_without_leaking_stderr(tmp_path):
    runner = DestroyRunner(
        plans={"destroy_sequence": [destroy_plan(AWS_ADDRESSES)]},
        apply_rc={"destroy.tfplan": 1},
        remaining=b"aws_vpc.target\n",
    )
    manifest, directory, planned = aws_plan(tmp_path, runner)
    result = aws_apply(manifest, directory, runner, planned["review"]["digest"])
    assert (result["status"], result["code"]) == ("FAILED", "TERRAFORM_DESTROY_FAILED")
    assert result["resource_refs"]["state_resource_count"] == "1"
    assert "abc" not in json.dumps(result)


def test_interrupted_destroy_reports_lock_and_keeps_state(tmp_path):
    runner = DestroyRunner(
        plans={"destroy_sequence": [destroy_plan(AWS_ADDRESSES)]},
        apply_rc={"destroy.tfplan": -1},
    )
    manifest, directory, planned = aws_plan(tmp_path, runner)
    result = aws_apply(manifest, directory, runner, planned["review"]["digest"])
    assert (result["status"], result["code"]) == ("UNKNOWN", "TERRAFORM_DESTROY_INTERRUPTED")
    refs = result["resource_refs"]
    assert (refs["stopped"], refs["state_lock"], refs["state_recovery"]) == (
        "TERMINATED",
        "ABSENT",
        "PRESERVED",
    )


def test_destroy_workspace_is_removed(tmp_path):
    runner = DestroyRunner(plans={"destroy_sequence": [destroy_plan(AWS_ADDRESSES)]})
    aws_plan(tmp_path, runner)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["plan-aws-r1"]


# ---- GCP (D6) ----

PROTECTED = [{"address": GCP_CLUSTER, "type": "google_container_cluster",
              "values": {"deletion_protection": True}}]  # fmt: skip
GCP_ADDRESSES = [GCP_CLUSTER, "google_compute_network.target"]


def gcp_call(fn, tmp_path, runner, **kwargs):
    manifest, _, directory = gcp_case.artifacts(tmp_path)
    return fn(
        manifest, directory, aws_profile="cmp-aws-prod", gcloud_configuration="cmp-gcp-prod",
        expected_adc_email=gcp_case.ADC, adc_email=lambda env: gcp_case.ADC, runner=runner,
        **kwargs,
    )  # fmt: skip


def gcp_runner(**kwargs):
    return DestroyRunner(
        account=gcp_case.STATE_ACCOUNT,
        plans={
            "destroy_sequence": [destroy_plan(GCP_ADDRESSES, prior=PROTECTED)],
            "unprotect.tfplan": unprotect_plan(),
        },
        **kwargs,
    )


def test_gke_deletion_protection_needs_a_single_attribute_unprotect_plan(tmp_path):
    runner = gcp_runner()
    result = gcp_call(plan_destroy, tmp_path, runner)
    assert (result["status"], result["code"]) == ("SUCCEEDED", "DESTROY_PLANNED")
    assert result["review"]["unprotect"] is True
    unprotect = next(c for c in runner.terraform() if c[0] == "plan" and "-destroy" not in c)
    assert "-var=deletion_protection=false" in unprotect
    assert not any(c[0] == "apply" for c in runner.terraform())


@pytest.mark.parametrize(
    "changes",
    [
        [{"address": GCP_CLUSTER, "change": {"actions": ["update"],
          "before": {"deletion_protection": True, "node_count": 1},
          "after": {"deletion_protection": False, "node_count": 2}}}],
        [{"address": "google_compute_network.target", "change": {"actions": ["delete", "create"]}}],
    ],
)  # fmt: skip
def test_unprotect_plan_with_any_other_change_is_unsafe(tmp_path, changes):
    runner = gcp_runner()
    runner.plans["unprotect.tfplan"] = unprotect_plan(changes)
    result = gcp_call(plan_destroy, tmp_path, runner)
    assert (result["status"], result["code"]) == ("FAILED", "UNPROTECT_PLAN_UNSAFE")


def test_confirmed_gke_destroy_unprotects_then_destroys(tmp_path):
    runner = gcp_runner()
    (tmp_path / "plan").mkdir()
    planned = gcp_call(plan_destroy, tmp_path / "plan", runner)
    after_unprotect = destroy_plan(GCP_ADDRESSES, prior=[dict(PROTECTED[0], values={})])
    runner.destroy_plans = [destroy_plan(GCP_ADDRESSES, prior=PROTECTED), after_unprotect]
    (tmp_path / "apply").mkdir()
    result = gcp_call(apply_destroy, tmp_path / "apply", runner,
                      review_digest=planned["review"]["digest"])  # fmt: skip
    assert (result["status"], result["code"]) == ("SUCCEEDED", "DESTROYED")
    applied = [c[-1] for c in runner.terraform() if c[0] == "apply"]
    assert applied == ["unprotect.tfplan", "destroy.tfplan"]


def test_gke_protection_still_on_after_unprotect_is_not_destroyed(tmp_path):
    runner = gcp_runner()
    (tmp_path / "plan").mkdir()
    (tmp_path / "apply").mkdir()
    planned = gcp_call(plan_destroy, tmp_path / "plan", runner)
    protected = destroy_plan(GCP_ADDRESSES, prior=PROTECTED)
    runner.destroy_plans = [protected, protected]
    digest = planned["review"]["digest"]
    result = gcp_call(apply_destroy, tmp_path / "apply", runner, review_digest=digest)
    assert (result["status"], result["code"]) == ("FAILED", "DESTROY_PLAN_CHANGED")
    assert [c[-1] for c in runner.terraform() if c[0] == "apply"] == ["unprotect.tfplan"]


def test_gcp_destroy_requires_the_expected_adc_principal(tmp_path):
    runner = gcp_runner()
    manifest, _, directory = gcp_case.artifacts(tmp_path)
    result = plan_destroy(
        manifest, directory, aws_profile="cmp-aws-prod", gcloud_configuration="cmp-gcp-prod",
        expected_adc_email=gcp_case.ADC, adc_email=lambda env: "other@example.com",
        runner=runner,
    )  # fmt: skip
    assert (result["status"], result["code"]) == ("FAILED", "ADC_PRINCIPAL_MISMATCH")
    assert not runner.commands
