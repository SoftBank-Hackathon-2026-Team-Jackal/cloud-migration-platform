"""Apply an approved, S3-backed saved GCP Terraform Plan (ticket 10, G2-G4).

Same guarantees as the AWS path (ticket 08): only the exact saved Plan pinned by the approved
manifest is applied, never re-planned or destroyed, and command output is never copied into
results. GCP-specific checks: the Plan was made in `S3` mode (not a local-State validation run),
the Google ADC principal is the expected one, and the AWS account that holds the central State
is the recorded one.
"""

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from tempfile import mkdtemp

from cmp_contracts.plan import PlanManifest, Preview
from cmp_contracts.provisioning import ProvisioningInputError
from pydantic import ValidationError

from cmp_providers.artifacts import verify_artifact_bytes
from cmp_providers.aws_apply import (
    APPLY_TIMEOUT_SECONDS,
    QUERY_TIMEOUT_SECONDS,
    CommandResult,
    Runner,
    subprocess_runner,
    template_evidence,
)
from cmp_providers.gcp_adc import adc_principal
from cmp_providers.gcp_preview import GcpPlanInput
from cmp_providers.resources import resource_root
from cmp_providers.terraform_events import apply_runner, is_stale

GCP_TEMPLATE_ROOT = resource_root() / "infra/terraform/gcp"
OUTPUT_REFS = {
    "project_id": "gcp_project_id",
    "network_name": "vpc_network",
    "subnetwork_name": "subnetwork",
    "cluster_name": "gke_cluster_name",
    "cluster_location": "gke_location",
    "node_pool_name": "node_pool_name",
}
AdcLookup = Callable[[dict[str, str]], str | None]


def default_adc_email(env: dict[str, str]) -> str | None:
    """Verified ADC principal (ticket 48 rules); None when it cannot be verified."""
    try:
        return adc_principal(env)
    except ProvisioningInputError:
        return None


def _result(status: str, code: str, refs: dict[str, str] | None = None) -> dict:
    return {"status": status, "code": code, "resource_refs": dict(refs or {})}


def _failed(code: str) -> dict:
    return _result("FAILED", code)


def _json(result: CommandResult):
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def _checked_inputs(manifest: PlanManifest, artifact_dir: Path, template_dir: Path):
    if manifest.target.provider != "GCP" or manifest.scope not in ("INFRASTRUCTURE", "FULL"):
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
        spec = GcpPlanInput.model_validate_json(spec_bytes)
    except (ValueError, ValidationError):
        return None, _failed("ARTIFACT_DIGEST_MISMATCH")
    try:
        recorded = json.loads((artifact_dir / "evidence.json").read_bytes())
    except (OSError, ValueError):
        return None, _failed("TEMPLATE_DRIFT")
    if recorded.get("plan_mode") != "S3":
        return None, _failed("LOCAL_STATE_PLAN")
    target = manifest.target
    if (
        spec.state_ref != target.state_ref
        or spec.environment_id != target.environment_id
        or spec.state_key != f"cmp/gcp/{target.environment_id}/terraform.tfstate"
    ):
        return None, _failed("STATE_MISMATCH")
    if (
        spec.project_id != target.account_id
        or spec.region != target.region
        or spec.plan != manifest.plan
    ):
        return None, _failed("TARGET_MISMATCH")
    current = template_evidence(template_dir)
    if any(recorded.get(key) != value for key, value in current.items()):
        return None, _failed("TEMPLATE_DRIFT")
    return (spec, plan_bytes), None


def apply_gcp_infrastructure(
    manifest: PlanManifest,
    preview: Preview,
    artifact_dir: Path,
    *,
    aws_profile: str,
    gcloud_configuration: str,
    expected_adc_email: str,
    terraform_bin: str = "terraform",
    runner: Runner = subprocess_runner,
    adc_email: AdcLookup = default_adc_email,
    template_dir: Path = GCP_TEMPLATE_ROOT,
    progress=None,
) -> dict:
    """Apply once. Callers must already hold the approval claim for this Plan Revision.

    `progress` (`terraform_events.ApplyProgress`) receives the `apply -json` stream (ticket 24).
    """
    del preview
    checked, failure = _checked_inputs(manifest, artifact_dir, template_dir)
    if failure:
        return failure
    spec, plan_bytes = checked
    if not aws_profile or aws_profile == "default" or not gcloud_configuration:
        return _failed("MISSING_PROFILE")
    if not expected_adc_email:
        return _failed("MISSING_ADC_PRINCIPAL")

    env = dict(
        os.environ,
        AWS_PROFILE=aws_profile,
        AWS_DEFAULT_PROFILE=aws_profile,
        CLOUDSDK_ACTIVE_CONFIG_NAME=gcloud_configuration,
        TF_IN_AUTOMATION="1",
        TF_INPUT="0",
    )
    for name in (
        "TF_LOG",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CREDENTIALS",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    ):
        env.pop(name, None)
    if adc_email(env) != expected_adc_email:
        return _failed("ADC_PRINCIPAL_MISMATCH")

    work = Path(mkdtemp(prefix="gcp-apply-", dir=artifact_dir.parent))
    os.chmod(work, 0o700)

    def terraform(args: list[str], timeout: int) -> CommandResult:
        return runner([terraform_bin, *args], work, env, timeout)

    def gcloud(args: list[str]) -> CommandResult:
        command = ["gcloud", *args, "--project", spec.project_id, "--format=json"]
        return runner(command, work, env, QUERY_TIMEOUT_SECONDS)

    try:
        identity = _json(
            runner(
                ["aws", "sts", "get-caller-identity", "--profile", aws_profile, "--output", "json"],
                work,
                env,
                QUERY_TIMEOUT_SECONDS,
            )
        )
        if not isinstance(identity, dict) or "Account" not in identity:
            return _failed("STATE_ACCOUNT_CHECK_FAILED")
        if identity["Account"] != spec.state_account_id:
            return _failed("STATE_ACCOUNT_MISMATCH")

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
                f"-backend-config=region={spec.state_region}",
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
            listed = terraform(["state", "list"], QUERY_TIMEOUT_SECONDS)
            if listed.returncode == 0:
                lines = listed.stdout.decode(errors="ignore").splitlines()
                refs = {"state_resource_count": str(len([line for line in lines if line]))}
            else:
                refs = {"state_inspection": "UNAVAILABLE"}
            if is_stale(applied, progress):
                return _result("UNKNOWN", "SAVED_PLAN_STALE", refs)
            if applied.returncode < 0:
                return _result("UNKNOWN", "TERRAFORM_APPLY_INTERRUPTED", refs)
            return _result("FAILED", "TERRAFORM_APPLY_FAILED", refs)

        outputs = _json(terraform(["output", "-json"], QUERY_TIMEOUT_SECONDS)) or {}
        refs = {}
        for name, ref_name in OUTPUT_REFS.items():
            item = outputs.get(name)
            if isinstance(item, dict) and not item.get("sensitive", True):
                value = item.get("value")
                if isinstance(value, str) and value:
                    refs[ref_name] = value
        cluster = refs.get("gke_cluster_name", spec.cluster_name)
        location = refs.get("gke_location", spec.region)
        described = _json(
            gcloud(["container", "clusters", "describe", cluster, "--location", location])
        )
        refs["gke_cluster_status"] = str((described or {}).get("status", "UNKNOWN"))
        if "node_pool_name" in refs:
            pool = _json(
                gcloud(
                    [
                        "container",
                        "node-pools",
                        "describe",
                        refs["node_pool_name"],
                        "--cluster",
                        cluster,
                        "--location",
                        location,
                    ]
                )
            )
            refs["node_pool_status"] = str((pool or {}).get("status", "UNKNOWN"))
        if refs["gke_cluster_status"] != "RUNNING" or refs.get("node_pool_status") != "RUNNING":
            return _result("FAILED", "CLUSTER_NOT_READY", refs)
        return _result("SUCCEEDED", "TERRAFORM_APPLIED", refs)
    finally:
        shutil.rmtree(work, ignore_errors=True)
