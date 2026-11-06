"""Reviewed Terraform destroy of an executed AWS/GCP INFRASTRUCTURE Plan (ticket 53, D5~D7).

`plan_destroy` rebuilds the reviewed variables from the approved Spec (its bytes are checked
against the manifest), runs `terraform plan -destroy` and refuses anything but deletes. The
review is the sorted list of addresses to delete plus whether GKE deletion protection must be
lifted first; its Digest is what an operator confirms. `apply_destroy` re-plans, requires the
same Digest, lifts protection with a separately verified one-attribute Plan when needed, re-plans
again (the earlier Plan is stale after that Apply), checks the address set once more and applies.

Runs as the target connection (`credential_ref`, ticket 46) with the S3 State backend on the
platform profile. Results carry codes, counts and resource addresses only; command output is
never copied (it may hold sensitive values).
"""

import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from cmp_contracts.plan import PlanManifest
from cmp_contracts.provisioning import ProvisioningInputError
from pydantic import ValidationError

from cmp_providers import aws_preview, gcp_preview
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes, verify_artifact_bytes
from cmp_providers.aws_apply import (
    APPLY_TIMEOUT_SECONDS,
    QUERY_TIMEOUT_SECONDS,
    TEMPLATE_ROOT,
    CommandResult,
    Runner,
    subprocess_runner,
    template_evidence,
)
from cmp_providers.aws_credentials import resolve_aws_profiles
from cmp_providers.gcp_apply import GCP_TEMPLATE_ROOT, AdcLookup, default_adc_email
from cmp_providers.terraform_process import failed_apply_refs

GKE_CLUSTER = "google_container_cluster"
GCP_SECRET_ENV = (
    "TF_LOG",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
)


class DestroyFailure(Exception):
    def __init__(self, code: str, status: str = "FAILED", refs: dict | None = None):
        super().__init__(code)
        self.code, self.status, self.refs = code, status, dict(refs or {})


def _result(status: str, code: str, refs: dict | None = None, review: dict | None = None):
    out = {"status": status, "code": code, "resource_refs": dict(refs or {})}
    if review is not None:
        out["review"] = review
    return out


def review_digest(environment_id: str, addresses: list[str], unprotect: bool) -> str:
    body = {"environment_id": environment_id, "delete": sorted(addresses), "unprotect": unprotect}
    return sha256_bytes(canonical_json_bytes(body))


@dataclass
class _Run:
    manifest: PlanManifest
    spec: object
    work: Path
    env: dict[str, str]
    runner: Runner
    terraform_bin: str
    keep_dir: Path
    label: str
    lock: dict[str, str]
    refs: dict[str, str]

    def terraform(self, args: list[str], timeout: int = QUERY_TIMEOUT_SECONDS) -> CommandResult:
        return self.runner([self.terraform_bin, *args], self.work, self.env, timeout)

    def show(self, plan_file: str) -> dict:
        shown = self.terraform(["show", "-json", plan_file])
        try:
            return json.loads(shown.stdout) if shown.returncode == 0 else {}
        except ValueError:
            return {}

    def state_count(self) -> dict[str, str]:
        listed = self.terraform(["state", "list"])
        if listed.returncode != 0:
            return {"state_inspection": "UNAVAILABLE"}
        lines = [line for line in listed.stdout.decode(errors="ignore").splitlines() if line]
        return {"state_resource_count": str(len(lines))}

    def apply(self, plan_file: str, failed: str, interrupted: str) -> None:
        applied = self.terraform(
            ["apply", "-input=false", "-no-color", "-lock=true", plan_file], APPLY_TIMEOUT_SECONDS
        )
        if applied.returncode == 0:
            return
        refs = self.state_count() | failed_apply_refs(
            applied,
            runner=self.runner,
            work=self.work,
            keep_dir=self.keep_dir,
            label=self.label,
            lock=self.lock,
            env=self.env,
        )
        raise DestroyFailure(interrupted if applied.returncode < 0 else failed,
                             "UNKNOWN" if applied.returncode < 0 else "FAILED", refs)  # fmt: skip


def _spec(manifest: PlanManifest, artifact_dir: Path):
    target = manifest.target
    if target.provider not in ("AWS", "GCP") or manifest.scope not in ("INFRASTRUCTURE", "FULL"):
        raise DestroyFailure("UNSUPPORTED_EXECUTION_SCOPE")
    ref = manifest.artifacts.get("spec")
    if ref is None:
        raise DestroyFailure("MISSING_ARTIFACT")
    try:
        spec_bytes = (artifact_dir / "spec.json").read_bytes()
    except OSError:
        raise DestroyFailure("ARTIFACT_NOT_FOUND") from None
    model = aws_preview.AwsPlanInput if target.provider == "AWS" else gcp_preview.GcpPlanInput
    try:
        verify_artifact_bytes(ref, spec_bytes)
        spec = model.model_validate_json(spec_bytes)
    except (ValueError, ValidationError):
        raise DestroyFailure("ARTIFACT_DIGEST_MISMATCH") from None
    if spec.state_bucket is None:
        raise DestroyFailure("LOCAL_STATE_PLAN")
    if spec.state_ref != target.state_ref or spec.environment_id != target.environment_id:
        raise DestroyFailure("STATE_MISMATCH")
    return spec


def _json(result: CommandResult):
    try:
        return json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        return None


@contextmanager
def _session(
    manifest: PlanManifest,
    artifact_dir: Path,
    *,
    aws_profile: str,
    gcloud_configuration: str | None,
    expected_adc_email: str | None,
    adc_email: AdcLookup,
    terraform_bin: str,
    runner: Runner,
    template_dir: Path | None,
) -> Iterator[_Run]:
    spec = _spec(manifest, artifact_dir)
    aws = manifest.target.provider == "AWS"
    template_dir = template_dir or (TEMPLATE_ROOT if aws else GCP_TEMPLATE_ROOT)
    try:
        variables = (aws_preview if aws else gcp_preview).terraform_variables(spec)
    except ProvisioningInputError as error:
        raise DestroyFailure(error.issues[0].code) from None
    base = dict(os.environ, TF_IN_AUTOMATION="1", TF_INPUT="0")
    if aws:
        try:
            profiles = resolve_aws_profiles(spec.credential_ref, platform_profile=aws_profile)
        except ProvisioningInputError as error:
            raise DestroyFailure(error.issues[0].code) from None
        env = base | {"AWS_PROFILE": profiles.provider, "AWS_DEFAULT_PROFILE": profiles.provider,
                      "AWS_REGION": spec.region}  # fmt: skip
        env.pop("TF_LOG", None)
        backend = [f"-backend-config=region={spec.region}",
                   f"-backend-config=profile={profiles.state}"]  # fmt: skip
        lock = {"bucket": spec.state_bucket, "key": spec.state_key, "region": spec.region,
                "profile": profiles.state}  # fmt: skip
        identity_args = ["--profile", profiles.provider, "--region", spec.region]
        expected_account, mismatch = manifest.target.account_id, "ACCOUNT_MISMATCH"
    else:
        if not aws_profile or aws_profile == "default" or not gcloud_configuration:
            raise DestroyFailure("MISSING_PROFILE")
        if not expected_adc_email:
            raise DestroyFailure("MISSING_ADC_PRINCIPAL")
        env = base | {"AWS_PROFILE": aws_profile, "AWS_DEFAULT_PROFILE": aws_profile,
                      "CLOUDSDK_ACTIVE_CONFIG_NAME": gcloud_configuration}  # fmt: skip
        for name in GCP_SECRET_ENV:
            env.pop(name, None)
        if adc_email(env) != expected_adc_email:  # same principal check as 10 (48 owns it)
            raise DestroyFailure("ADC_PRINCIPAL_MISMATCH")
        backend = [f"-backend-config=region={spec.state_region}"]
        lock = {"bucket": spec.state_bucket, "key": spec.state_key, "region": spec.state_region,
                "profile": aws_profile}  # fmt: skip
        identity_args = ["--profile", aws_profile]
        expected_account, mismatch = spec.state_account_id, "STATE_ACCOUNT_MISMATCH"

    work = Path(mkdtemp(prefix="cloud-destroy-", dir=artifact_dir.parent))
    os.chmod(work, 0o700)
    try:
        identity = _json(
            runner(
                ["aws", "sts", "get-caller-identity", *identity_args, "--output", "json"],
                work,
                env,
                QUERY_TIMEOUT_SECONDS,
            )
        )
        if not isinstance(identity, dict) or "Account" not in identity:
            raise DestroyFailure("ACCOUNT_CHECK_FAILED")
        if identity["Account"] != expected_account:
            raise DestroyFailure(mismatch)
        for source in template_dir.glob("*.tf"):
            shutil.copy2(source, work / source.name)
        shutil.copy2(template_dir / ".terraform.lock.hcl", work / ".terraform.lock.hcl")
        var_file = work / "input.auto.tfvars.json"
        var_file.write_bytes(canonical_json_bytes(variables))
        os.chmod(var_file, 0o600)
        refs = {}
        try:
            recorded = json.loads((artifact_dir / "evidence.json").read_bytes())
            current = template_evidence(template_dir)
            drift = any(recorded.get(key) != value for key, value in current.items())
        except (OSError, ValueError):
            drift = True
        if drift:  # D5: reported, never blocking — destroy reads the State, not the template
            refs["template_drift"] = "true"
        run = _Run(manifest, spec, work, env, runner, terraform_bin,
                   artifact_dir.parent / "state-recovery", f"{artifact_dir.name}-destroy", lock,
                   refs)  # fmt: skip
        init = run.terraform(
            ["init", "-input=false", "-lockfile=readonly",
             f"-backend-config=bucket={spec.state_bucket}", f"-backend-config=key={spec.state_key}",
             "-backend-config=encrypt=true", "-backend-config=use_lockfile=true", *backend]
        )  # fmt: skip
        if init.returncode != 0:
            raise DestroyFailure("TERRAFORM_INIT_FAILED")
        yield run
    finally:
        shutil.rmtree(work, ignore_errors=True)


def destroy_addresses(plan_json: dict) -> list[str]:
    """Sorted addresses a `terraform show -json` destroy Plan deletes; ValueError on any other
    change (create/update/replace/read)."""
    addresses = []
    for change in plan_json.get("resource_changes") or []:
        actions = (change.get("change") or {}).get("actions") or []
        if actions == ["no-op"]:
            continue
        if actions != ["delete"]:
            raise ValueError(change.get("address"))
        addresses.append(change["address"])
    return sorted(addresses)


def _destroy_review(run: _Run) -> tuple[list[str], bool]:
    """(addresses to delete, GKE protection must be lifted) from a fresh `plan -destroy`."""
    planned = run.terraform(
        ["plan", "-destroy", "-input=false", "-lock=true", "-no-color", "-out=destroy.tfplan"]
    )
    if planned.returncode != 0:
        raise DestroyFailure("DESTROY_PLAN_FAILED", refs=run.refs)
    shown = run.show("destroy.tfplan")
    if "resource_changes" not in shown and "prior_state" not in shown:
        raise DestroyFailure("DESTROY_PLAN_FAILED", refs=run.refs)
    try:
        addresses = destroy_addresses(shown)
    except ValueError:
        raise DestroyFailure("DESTROY_PLAN_UNSAFE", refs=run.refs) from None
    resources = ((shown.get("prior_state") or {}).get("values") or {}).get("root_module") or {}
    protected = any(
        item.get("type") == GKE_CLUSTER and (item.get("values") or {}).get("deletion_protection")
        for item in resources.get("resources") or []
    )
    return addresses, protected


def _unprotect_plan(run: _Run) -> None:
    """D6: a Plan whose only change is `deletion_protection` true → false on the GKE cluster."""
    planned = run.terraform(
        ["plan", "-input=false", "-lock=true", "-no-color", "-var=deletion_protection=false",
         "-out=unprotect.tfplan"]
    )  # fmt: skip
    if planned.returncode != 0:
        raise DestroyFailure("UNPROTECT_PLAN_FAILED", refs=run.refs)
    changed = [
        change
        for change in run.show("unprotect.tfplan").get("resource_changes") or []
        if ((change.get("change") or {}).get("actions") or []) != ["no-op"]
    ]
    if len(changed) != 1 or not changed[0].get("address", "").startswith(f"{GKE_CLUSTER}."):
        raise DestroyFailure("UNPROTECT_PLAN_UNSAFE", refs=run.refs)
    body = changed[0].get("change") or {}
    before, after = body.get("before") or {}, body.get("after") or {}
    diff = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if body.get("actions") != ["update"] or diff != {"deletion_protection"}:
        raise DestroyFailure("UNPROTECT_PLAN_UNSAFE", refs=run.refs)


def _review(run: _Run) -> dict:
    addresses, protected = _destroy_review(run)
    if protected:
        _unprotect_plan(run)
    environment = run.manifest.target.environment_id
    return {
        "digest": review_digest(environment, addresses, protected),
        "addresses": addresses,
        "unprotect": protected,
    }


def plan_destroy(
    manifest: PlanManifest,
    artifact_dir: Path,
    *,
    aws_profile: str,
    gcloud_configuration: str | None = None,
    expected_adc_email: str | None = None,
    adc_email: AdcLookup = default_adc_email,
    terraform_bin: str = "terraform",
    runner: Runner = subprocess_runner,
    template_dir: Path | None = None,
) -> dict:
    """Read-only (besides the State lock while planning): the review an operator confirms."""
    refs = {}
    try:
        with _session(manifest, artifact_dir, aws_profile=aws_profile,
                      gcloud_configuration=gcloud_configuration,
                      expected_adc_email=expected_adc_email, adc_email=adc_email,
                      terraform_bin=terraform_bin, runner=runner,
                      template_dir=template_dir) as run:  # fmt: skip
            refs = run.refs
            review = _review(run)
    except DestroyFailure as failure:
        return _result(failure.status, failure.code, refs | failure.refs)
    refs |= {"delete_count": str(len(review["addresses"])),
             "unprotect": str(review["unprotect"]).lower()}  # fmt: skip
    code = "DESTROY_PLANNED" if review["addresses"] else "NOTHING_TO_DESTROY"
    return _result("SUCCEEDED", code, refs, review)


def apply_destroy(
    manifest: PlanManifest,
    artifact_dir: Path,
    *,
    review_digest: str,
    aws_profile: str,
    gcloud_configuration: str | None = None,
    expected_adc_email: str | None = None,
    adc_email: AdcLookup = default_adc_email,
    terraform_bin: str = "terraform",
    runner: Runner = subprocess_runner,
    template_dir: Path | None = None,
) -> dict:
    """Destroy only when a fresh review still has the confirmed Digest (D5)."""
    refs = {}
    try:
        with _session(manifest, artifact_dir, aws_profile=aws_profile,
                      gcloud_configuration=gcloud_configuration,
                      expected_adc_email=expected_adc_email, adc_email=adc_email,
                      terraform_bin=terraform_bin, runner=runner,
                      template_dir=template_dir) as run:  # fmt: skip
            refs = run.refs
            review = _review(run)
            if review["digest"] != review_digest:
                raise DestroyFailure("DESTROY_REVIEW_MISMATCH")
            if not review["addresses"]:
                return _result("SUCCEEDED", "NOTHING_TO_DESTROY", refs)
            if review["unprotect"]:
                run.apply("unprotect.tfplan", "UNPROTECT_APPLY_FAILED", "UNPROTECT_INTERRUPTED")
                refs["unprotect"] = "applied"
                addresses, protected = _destroy_review(run)
                if addresses != review["addresses"] or protected:
                    raise DestroyFailure("DESTROY_PLAN_CHANGED")
            run.apply("destroy.tfplan", "TERRAFORM_DESTROY_FAILED", "TERRAFORM_DESTROY_INTERRUPTED")
            remaining = run.state_count()
            refs |= remaining
            if remaining.get("state_resource_count") not in ("0", None):
                raise DestroyFailure("DESTROY_INCOMPLETE", "UNKNOWN")
    except DestroyFailure as failure:
        return _result(failure.status, failure.code, refs | failure.refs)
    refs["destroyed_count"] = str(len(review["addresses"]))
    return _result("SUCCEEDED", "DESTROYED", refs)
