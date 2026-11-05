"""Apply an approved, S3-backed saved AWS Terraform Plan (ticket 08, E2-E5).

Only the exact saved Plan bytes pinned by the approved manifest are applied; nothing is
re-planned or destroyed. Before Terraform runs, the stored Spec/Plan bytes, the state identity,
the template/lockfile fingerprint recorded by Planning (05) and the live AWS account are checked.
After Apply, only non-sensitive Terraform outputs and EKS status are returned. Command output
(stdout/stderr) is never copied into results because it may contain sensitive values.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from cmp_contracts.plan import PlanManifest, Preview
from pydantic import ValidationError

from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes, verify_artifact_bytes
from cmp_providers.aws_preview import AwsPlanInput
from cmp_providers.resources import resource_root
from cmp_providers.terraform_events import apply_runner, is_stale

TEMPLATE_ROOT = resource_root() / "infra/terraform/aws"
APPLY_TIMEOUT_SECONDS = 90 * 60
QUERY_TIMEOUT_SECONDS = 10 * 60
# Terraform output name -> result reference name. Anything else (and anything sensitive) is
# dropped.
OUTPUT_REFS = {
    "vpc_id": "vpc_id",
    "public_subnet_ids": "public_subnet_ids",
    "private_subnet_ids": "private_subnet_ids",
    "cluster_name": "eks_cluster_name",
    "cluster_arn": "eks_cluster_arn",
    "node_group_name": "node_group_name",
    "oidc_provider_arn": "oidc_provider_arn",
    "ebs_csi_role_arn": "ebs_csi_role_arn",
    "lb_controller_role_arn": "lb_controller_role_arn",
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[list[str], Path, dict[str, str], int], CommandResult]


def subprocess_runner(args: list[str], cwd: Path, env: dict[str, str], timeout: int):
    try:
        done = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return CommandResult(-1, b"", b"")
    return CommandResult(done.returncode, done.stdout, done.stderr)


def template_evidence(root: Path = TEMPLATE_ROOT) -> dict[str, str]:
    """Same fingerprint fields that 05 writes to `evidence.json` at Plan time."""
    digests = {path.name: sha256_bytes(path.read_bytes()) for path in root.glob("*.tf")}
    return {
        "template_digest": sha256_bytes(canonical_json_bytes(digests)),
        "provider_lock_digest": sha256_bytes((root / ".terraform.lock.hcl").read_bytes()),
    }


def _result(status: str, code: str, refs: dict[str, str] | None = None) -> dict:
    return {"status": status, "code": code, "resource_refs": dict(refs or {})}


def _failed(code: str) -> dict:
    return _result("FAILED", code)


def _checked_inputs(manifest: PlanManifest, artifact_dir: Path, template_dir: Path):
    """Return (spec, plan_bytes) or a FAILED result; never runs a command."""
    if manifest.target.provider != "AWS" or manifest.scope not in ("INFRASTRUCTURE", "FULL"):
        return None, _failed("UNSUPPORTED_EXECUTION_SCOPE")
    refs = manifest.artifacts
    if "spec" not in refs or "terraform_plan" not in refs:
        return None, _failed("MISSING_ARTIFACT")
    try:
        spec_bytes = (artifact_dir / "spec.json").read_bytes()
        plan_bytes = (artifact_dir / "saved.tfplan").read_bytes()
    except OSError:
        return None, _failed("ARTIFACT_NOT_FOUND")
    try:
        verify_artifact_bytes(refs["spec"], spec_bytes)
        verify_artifact_bytes(refs["terraform_plan"], plan_bytes)
    except ValueError:
        return None, _failed("ARTIFACT_DIGEST_MISMATCH")
    try:
        spec = AwsPlanInput.model_validate_json(spec_bytes)
    except ValidationError:
        return None, _failed("ARTIFACT_DIGEST_MISMATCH")
    target = manifest.target
    if spec.state_bucket is None:
        return None, _failed("LOCAL_STATE_PLAN")
    if (
        spec.state_ref != target.state_ref
        or spec.environment_id != target.environment_id
        or spec.state_key != f"cmp/{target.environment_id}/terraform.tfstate"
    ):
        return None, _failed("STATE_MISMATCH")
    if (
        spec.account_id != target.account_id
        or spec.region != target.region
        or spec.plan != manifest.plan
    ):
        return None, _failed("TARGET_MISMATCH")
    try:
        recorded = json.loads((artifact_dir / "evidence.json").read_bytes())
    except (OSError, ValueError):
        return None, _failed("TEMPLATE_DRIFT")
    current = template_evidence(template_dir)
    if any(recorded.get(key) != value for key, value in current.items()):
        return None, _failed("TEMPLATE_DRIFT")
    return (spec, plan_bytes), None


def _json(result: CommandResult):
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def _state_count(run) -> dict[str, str]:
    listed = run(["state", "list"], QUERY_TIMEOUT_SECONDS)
    if listed.returncode != 0:
        return {"state_inspection": "UNAVAILABLE"}
    count = len([line for line in listed.stdout.decode(errors="ignore").splitlines() if line])
    return {"state_resource_count": str(count)}


def _output_refs(outputs: dict | None) -> dict[str, str]:
    refs = {}
    for name, ref_name in OUTPUT_REFS.items():
        item = (outputs or {}).get(name)
        if not isinstance(item, dict) or item.get("sensitive", True):
            continue
        value = item.get("value")
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        if isinstance(value, str) and value:
            refs[ref_name] = value
    return refs


def apply_aws_infrastructure(
    manifest: PlanManifest,
    preview: Preview,
    artifact_dir: Path,
    *,
    aws_profile: str,
    terraform_bin: str = "terraform",
    runner: Runner = subprocess_runner,
    template_dir: Path = TEMPLATE_ROOT,
    progress=None,
) -> dict:
    """Apply once. Callers must already hold the approval claim for this Plan Revision.

    `progress` (`terraform_events.ApplyProgress`) receives the `apply -json` stream (ticket 24).

    `preview` is accepted for symmetry with the approval contract; approval issues (including
    LOCAL_STATE_* markers) are rejected earlier by the claim, and the Spec is re-checked here.
    """
    del preview
    checked, failure = _checked_inputs(manifest, artifact_dir, template_dir)
    if failure:
        return failure
    spec, plan_bytes = checked
    if not aws_profile or aws_profile == "default":
        return _failed("MISSING_PROFILE")

    env = dict(
        os.environ,
        AWS_PROFILE=aws_profile,
        AWS_DEFAULT_PROFILE=aws_profile,
        AWS_REGION=spec.region,
        TF_IN_AUTOMATION="1",
        TF_INPUT="0",
    )
    env.pop("TF_LOG", None)
    work = Path(mkdtemp(prefix="aws-apply-", dir=artifact_dir.parent))
    os.chmod(work, 0o700)

    def aws(args: list[str]) -> CommandResult:
        command = ["aws", *args, "--profile", aws_profile, "--region", spec.region]
        return runner([*command, "--output", "json"], work, env, QUERY_TIMEOUT_SECONDS)

    def terraform(args: list[str], timeout: int) -> CommandResult:
        return runner([terraform_bin, *args], work, env, timeout)

    try:
        identity = _json(aws(["sts", "get-caller-identity"]))
        if not isinstance(identity, dict) or "Account" not in identity:
            return _failed("ACCOUNT_CHECK_FAILED")
        if identity["Account"] != manifest.target.account_id:
            return _failed("ACCOUNT_MISMATCH")

        for source in template_dir.glob("*.tf"):
            shutil.copy2(source, work / source.name)
        shutil.copy2(template_dir / ".terraform.lock.hcl", work / ".terraform.lock.hcl")
        (work / "saved.tfplan").write_bytes(plan_bytes)
        os.chmod(work / "saved.tfplan", 0o600)

        init = terraform(
            [
                "init",
                "-input=false",
                "-lockfile=readonly",
                f"-backend-config=bucket={spec.state_bucket}",
                f"-backend-config=key={spec.state_key}",
                f"-backend-config=region={spec.region}",
                "-backend-config=encrypt=true",
                "-backend-config=use_lockfile=true",
            ],
            QUERY_TIMEOUT_SECONDS,
        )
        if init.returncode != 0:
            return _failed("TERRAFORM_INIT_FAILED")

        applied = apply_runner(runner, progress)(
            [terraform_bin, "apply", "-input=false", "-no-color", "-json", "-lock=true"]
            + ["saved.tfplan"],
            work,
            env,
            APPLY_TIMEOUT_SECONDS,
        )
        if applied.returncode != 0:
            refs = _state_count(lambda args, t: terraform(args, t))
            if is_stale(applied, progress):
                # The State changed since Planning (possibly our own lost Apply). Inspect only.
                return _result("UNKNOWN", "SAVED_PLAN_STALE", refs)
            if applied.returncode < 0:
                return _result("UNKNOWN", "TERRAFORM_APPLY_INTERRUPTED", refs)
            return _result("FAILED", "TERRAFORM_APPLY_FAILED", refs)

        refs = _output_refs(_json(terraform(["output", "-json"], QUERY_TIMEOUT_SECONDS)))
        cluster_name = refs.get("eks_cluster_name", spec.cluster_name)
        cluster = _json(aws(["eks", "describe-cluster", "--name", cluster_name])) or {}
        refs["eks_cluster_status"] = str(cluster.get("cluster", {}).get("status", "UNKNOWN"))
        if "node_group_name" in refs:
            group = _json(
                aws(
                    [
                        "eks",
                        "describe-nodegroup",
                        "--cluster-name",
                        cluster_name,
                        "--nodegroup-name",
                        refs["node_group_name"],
                    ]
                )
            )
            refs["node_group_status"] = str((group or {}).get("nodegroup", {}).get("status"))
        if refs["eks_cluster_status"] != "ACTIVE" or refs.get("node_group_status") != "ACTIVE":
            return _result("FAILED", "CLUSTER_NOT_READY", refs)
        return _result("SUCCEEDED", "TERRAFORM_APPLIED", refs)
    finally:
        shutil.rmtree(work, ignore_errors=True)
