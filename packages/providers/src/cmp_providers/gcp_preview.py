"""GCP infrastructure-only Terraform Plan and allowlisted Preview boundary."""

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from tempfile import mkdtemp
from typing import Annotated, Literal

from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.plan import CloudIdentity, PlanManifest, Preview, ResourceChange
from cmp_contracts.provisioning import (
    Contract,
    PlanRef,
    ProvisioningInputError,
    Text,
    ValidationIssue,
    VersionRef,
)
from pydantic import Field

from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes
from cmp_providers.plans import manifest_digest

_NAME = r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$"
_GCP_PROJECT = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
_RESOURCE_TYPES = {
    "google_compute_network": ("NETWORK", "VPC Network"),
    "google_compute_subnetwork": ("NETWORK", "Regional subnet"),
    "google_compute_router": ("NETWORK", "Cloud Router"),
    "google_compute_router_nat": ("NETWORK", "Cloud NAT"),
    "google_compute_firewall": ("NETWORK", "Internal firewall"),
    "google_container_cluster": ("CLUSTER", "GKE cluster"),
    "google_container_node_pool": ("NODE", "GKE node pool"),
}
_RFC1918 = tuple(ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class GcpPlanInput(Contract):
    """Reviewed target configuration without credential or Secret bytes."""

    migration_id: Text
    plan: PlanRef
    environment_id: Text
    project_id: Annotated[str, Field(pattern=_GCP_PROJECT)]
    region: Annotated[str, Field(pattern=r"^[a-z]+(?:-[a-z]+)+[0-9]+$")]
    credential_ref: VersionRef
    state_ref: VersionRef
    state_bucket: Text
    state_key: Text
    state_region: Annotated[str, Field(pattern=r"^[a-z]{2}(?:-[a-z]+)+-[0-9]$")]
    state_account_id: Annotated[str, Field(pattern=r"^[0-9]{12}$")]
    cluster_name: Annotated[str, Field(pattern=_NAME)]
    subnet_cidr: Text
    pods_cidr: Text
    services_cidr: Text
    master_cidr: Text
    api_access_cidrs: list[Text] = Field(min_length=1)
    release_channel: Literal["REGULAR", "STABLE"]
    node_machine_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
    node_count: int = Field(ge=1, le=20)
    node_disk_gib: int = Field(ge=20, le=1024)
    node_service_account_email: Annotated[
        str, Field(pattern=r"^[a-z][a-z0-9-]+@[a-z][a-z0-9-]+\.iam\.gserviceaccount\.com$")
    ]


def _reject(code: str, path: str, message: str) -> None:
    raise ProvisioningInputError([ValidationIssue(code=code, path=path, message=message)])


def _network(value: str, path: str, *, rfc1918: bool) -> IPv4Network:
    try:
        result = ip_network(value, strict=True)
        if not isinstance(result, IPv4Network):
            raise ValueError("IPv6 unsupported")  # noqa: TRY004
    except ValueError:
        _reject("INVALID_CIDR", path, "Expected a canonical IPv4 CIDR.")
    if rfc1918 and not any(result.subnet_of(private) for private in _RFC1918):
        _reject("INVALID_TOPOLOGY", path, "GKE IP ranges must be RFC1918 private space.")
    return result


def terraform_variables(request: GcpPlanInput) -> dict:
    """Map the reviewed GCP topology to fixed Terraform inputs, without I/O."""
    if request.plan.template_version != "gcp-gke-v2":
        _reject(
            "UNSUPPORTED_TEMPLATE", "plan.template_version", "Unsupported GCP template version."
        )
    for path, value in (
        ("plan.plan_id", request.plan.plan_id),
        ("environment_id", request.environment_id),
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            _reject("INVALID_IDENTIFIER", path, "Expected a safe opaque identifier.")
    if request.state_key != f"cmp/gcp/{request.environment_id}/terraform.tfstate":
        _reject("INVALID_STATE_KEY", "state_key", "State key must be stable per environment.")
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", request.state_bucket)
        or ".." in request.state_bucket
    ):
        _reject("INVALID_STATE_BUCKET", "state_bucket", "Expected an existing S3 bucket name.")
    if not request.node_service_account_email.endswith(
        f"@{request.project_id}.iam.gserviceaccount.com"
    ):
        _reject(
            "NODE_SA_PROJECT_MISMATCH",
            "node_service_account_email",
            "P0 node service account must belong to the target project.",
        )
    ranges = {
        path: _network(getattr(request, path), path, rfc1918=True)
        for path in ("subnet_cidr", "pods_cidr", "services_cidr", "master_cidr")
    }
    if ranges["subnet_cidr"].prefixlen > 27:
        _reject("INVALID_TOPOLOGY", "subnet_cidr", "Primary subnet must be at least /27.")
    if ranges["master_cidr"].prefixlen != 28:
        _reject("INVALID_TOPOLOGY", "master_cidr", "Private GKE control plane needs a /28.")
    for index, (left_name, left) in enumerate(ranges.items()):
        for right_name, right in list(ranges.items())[index + 1 :]:
            if left.overlaps(right):
                _reject("CIDR_OVERLAP", right_name, f"{left_name} and {right_name} overlap.")
    for index, cidr in enumerate(request.api_access_cidrs):
        network = _network(cidr, f"api_access_cidrs.{index}", rfc1918=False)
        if network.prefixlen == 0:
            _reject("PUBLIC_API_EXPOSURE", f"api_access_cidrs.{index}", "Restrict GKE API access.")
    return {
        "project_id": request.project_id,
        "region": request.region,
        "name": request.cluster_name,
        "subnet_cidr": request.subnet_cidr,
        "pods_cidr": request.pods_cidr,
        "services_cidr": request.services_cidr,
        "master_cidr": request.master_cidr,
        "api_access_cidrs": request.api_access_cidrs,
        "release_channel": request.release_channel,
        "node_machine_type": request.node_machine_type,
        "node_count": request.node_count,
        "node_disk_gib": request.node_disk_gib,
        "node_service_account_email": request.node_service_account_email,
    }


def preview_from_plan(plan_json: dict, request: GcpPlanInput, digest: str) -> Preview:
    """Only fixed resource types and reviewed request fields enter the Preview."""
    counts: dict[str, int] = {}
    for change in plan_json.get("resource_changes", []):
        kind = change.get("type")
        if change.get("mode") != "managed" or kind not in _RESOURCE_TYPES:
            _reject("UNREVIEWED_PLAN", "terraform_plan", "Plan contains an unexpected resource.")
        if change.get("change", {}).get("actions") != ["create"]:
            _reject(
                "NON_CREATE_PLAN", "terraform_plan", "New-network Plan must only create resources."
            )
        counts[kind] = counts.get(kind, 0) + 1
    if set(counts) != set(_RESOURCE_TYPES) or any(count != 1 for count in counts.values()):
        _reject(
            "INCOMPLETE_NEW_PLAN", "terraform_plan", "Expected one of each reviewed GCP resource."
        )
    display = {
        "VPC Network": "Custom regional routing; no auto-created subnets",
        "Regional subnet": (
            f"{request.region} primary {request.subnet_cidr}; "
            f"Pods {request.pods_cidr}; Services {request.services_cidr}"
        ),
        "Cloud NAT": "Private node outbound via regional Cloud NAT",
        "Internal firewall": "Internal primary/Pod CIDRs only",
        "GKE cluster": (
            f"Regional Standard; {request.release_channel} channel; private nodes; "
            f"control plane {request.master_cidr}; public API restricted to "
            f"{', '.join(request.api_access_cidrs)}"
        ),
        "GKE node pool": (
            f"{request.node_machine_type}; {request.node_count} nodes; "
            f"{request.node_disk_gib} GiB each; configured node service account "
            "(existence unverified)"
        ),
    }
    changes = [
        ResourceChange(
            resource_type=_RESOURCE_TYPES[kind][0],
            action="CREATE",
            name=_RESOURCE_TYPES[kind][1],
            quantity=count,
            size=display.get(_RESOURCE_TYPES[kind][1]),
        )
        for kind, count in sorted(counts.items())
    ]
    return Preview(
        contract_version="1",
        manifest_digest=digest,
        changes=changes,
        applied_defaults=[
            ResourceChange(
                resource_type="CLUSTER",
                action="CREATE",
                name="GKE default node pool removed; deletion protection enabled",
            ),
            ResourceChange(
                resource_type="NETWORK",
                action="CREATE",
                name="Private Google Access enabled on regional subnet",
            ),
        ],
        warnings=[
            ValidationIssue(
                code="NODE_SA_PRECONDITION",
                path="node_service_account_email",
                message="Pre-existing node service account and required IAM roles must be verified.",
            ),
            ValidationIssue(
                code="INGRESS_NOT_DEPLOYED",
                path="external_access",
                message="Ingress, TLS, workload image pull and storage are outside this Plan.",
            ),
        ],
    )


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> bytes:
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, check=False, timeout=600
        )
    except (OSError, subprocess.TimeoutExpired):
        _reject("TOOL_UNAVAILABLE", "plan", "A required Plan tool could not complete.")
    if result.returncode:
        # Provider and CLI output may contain state, paths or credentials; never surface it.
        _reject("PLAN_COMMAND_FAILED", "plan", f"Plan command failed at {args[1]}.")
    return result.stdout


def _verify_adc(expected_email: str, work: Path, env: dict[str, str]) -> None:
    if env.get("GOOGLE_APPLICATION_CREDENTIALS"):
        _reject(
            "ADC_SOURCE_UNSUPPORTED",
            "adc",
            "This local runner requires gcloud user ADC, not an override credential file.",
        )
    token = _run(["gcloud", "auth", "application-default", "print-access-token"], work, env).strip()
    if not token:
        _reject("ADC_UNAVAILABLE", "adc", "Google ADC token is unavailable.")
    try:
        body = urllib.parse.urlencode({"access_token": token.decode("ascii")}).encode()
        with urllib.request.urlopen(
            urllib.request.Request("https://oauth2.googleapis.com/tokeninfo", data=body),
            timeout=15,
        ) as response:
            identity = json.load(response)
    except (OSError, ValueError, UnicodeError, urllib.error.HTTPError):
        _reject("ADC_CHECK_FAILED", "adc", "Google ADC identity could not be verified.")
    if identity.get("email") != expected_email:
        _reject("ADC_PRINCIPAL_MISMATCH", "adc", "Google ADC principal does not match expectation.")


def plan_gcp_infrastructure(
    request: GcpPlanInput,
    *,
    output_dir: Path,
    mode: Literal["S3", "LOCAL_TEST", "LOCAL_PROJECT"] = "S3",
    gcloud_configuration: str | None = None,
    expected_adc_email: str | None = None,
    aws_profile: str | None = None,
    terraform_bin: str = "terraform",
    template_dir: Path | None = None,
) -> tuple[PlanManifest, Preview]:
    """Run a saved Plan; local test output is explicitly unapprovable."""
    variables = terraform_variables(request)
    if mode == "LOCAL_TEST":
        if request.project_id != "cmp-plan-local":
            _reject(
                "INVALID_TEST_TARGET",
                "project_id",
                "Local test mode requires a fixed fake project.",
            )
    elif mode in ("S3", "LOCAL_PROJECT"):
        for path, value in (
            ("gcloud_configuration", gcloud_configuration),
            ("expected_adc_email", expected_adc_email),
        ):
            if not value or value == "default":
                _reject("MISSING_IDENTITY", path, "Explicit named identity is required.")
        if mode == "S3" and (not aws_profile or aws_profile == "default"):
            _reject("MISSING_IDENTITY", "aws_profile", "Explicit named State identity is required.")
    else:
        _reject("UNSUPPORTED_PLAN_MODE", "mode", "Unknown Plan mode.")
    root = template_dir or Path(__file__).resolve().parents[4] / "infra/terraform/gcp"
    lock = root / ".terraform.lock.hcl"
    if not lock.exists():
        _reject("MISSING_PROVIDER_LOCK", "terraform", "A reviewed provider lockfile is required.")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    target = output_dir / f"{request.plan.plan_id}-r{request.plan.revision}"
    if target.exists() or target.is_symlink():
        _reject("IMMUTABLE_PLAN_EXISTS", "output_dir", "Plan revision already exists.")
    work = Path(mkdtemp(prefix="gcp-plan-", dir=output_dir))
    os.chmod(work, 0o700)
    try:
        template_digests = {}
        for source in root.glob("*.tf"):
            shutil.copy2(source, work / source.name)
            template_digests[source.name] = sha256_bytes(source.read_bytes())
        if mode != "S3":
            versions = work / "versions.tf"
            content = versions.read_text()
            if content.count('backend "s3" {}') != 1:
                _reject(
                    "UNREVIEWED_TEMPLATE", "terraform", "Expected exactly one S3 backend block."
                )
            versions.write_text(content.replace('backend "s3" {}', 'backend "local" {}'))
        shutil.copy2(lock, work / lock.name)
        var_file = work / "input.auto.tfvars.json"
        var_file.write_bytes(canonical_json_bytes(variables))
        os.chmod(var_file, 0o600)
        env = dict(os.environ, TF_IN_AUTOMATION="1", TF_INPUT="0")
        env.pop("TF_LOG", None)
        if mode == "LOCAL_TEST":
            for name in (
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_CREDENTIALS",
                "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
                "AWS_PROFILE",
                "AWS_DEFAULT_PROFILE",
            ):
                env.pop(name, None)
            env["GOOGLE_OAUTH_ACCESS_TOKEN"] = "cmp-local-test-invalid-token"
            _run([terraform_bin, "init", "-input=false", "-lockfile=readonly"], work, env)
        else:
            for name in (
                "GOOGLE_OAUTH_ACCESS_TOKEN",
                "GOOGLE_CREDENTIALS",
                "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_SECURITY_TOKEN",
                "AWS_WEB_IDENTITY_TOKEN_FILE",
                "AWS_ROLE_ARN",
            ):
                env.pop(name, None)
            env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = gcloud_configuration
            if mode == "S3":
                env["AWS_PROFILE"] = aws_profile
                env["AWS_DEFAULT_PROFILE"] = aws_profile
            else:
                env.pop("AWS_PROFILE", None)
                env.pop("AWS_DEFAULT_PROFILE", None)
            project = (
                _run(
                    [
                        "gcloud",
                        "config",
                        "get-value",
                        "project",
                        "--configuration",
                        gcloud_configuration,
                    ],
                    work,
                    env,
                )
                .decode()
                .strip()
            )
            if project != request.project_id:
                _reject(
                    "PROJECT_MISMATCH",
                    "project_id",
                    "gcloud configuration selects another project.",
                )
            if mode == "S3":
                account = _run(
                    [
                        "aws",
                        "sts",
                        "get-caller-identity",
                        "--profile",
                        aws_profile,
                        "--output",
                        "json",
                    ],
                    work,
                    env,
                )
                try:
                    state_account = json.loads(account)["Account"]
                except (ValueError, KeyError, TypeError):
                    _reject(
                        "STATE_ACCOUNT_CHECK_FAILED",
                        "state_account_id",
                        "AWS State identity is unavailable.",
                    )
                if state_account != request.state_account_id:
                    _reject(
                        "STATE_ACCOUNT_MISMATCH",
                        "state_account_id",
                        "S3 profile selects another account.",
                    )
            _verify_adc(expected_adc_email, work, env)
            if mode == "LOCAL_PROJECT":
                _run([terraform_bin, "init", "-input=false", "-lockfile=readonly"], work, env)
            else:
                _run(
                    [
                        terraform_bin,
                        "init",
                        "-input=false",
                        "-lockfile=readonly",
                        f"-backend-config=bucket={request.state_bucket}",
                        f"-backend-config=key={request.state_key}",
                        f"-backend-config=region={request.state_region}",
                        f"-backend-config=profile={aws_profile}",
                        "-backend-config=encrypt=true",
                        "-backend-config=use_lockfile=true",
                    ],
                    work,
                    env,
                )
        _run([terraform_bin, "validate", "-no-color"], work, env)
        plan_file = work / "saved.tfplan"
        plan_options = ["-refresh=false"] if mode != "S3" else []
        _run(
            [
                terraform_bin,
                "plan",
                "-input=false",
                "-lock=true",
                "-no-color",
                *plan_options,
                "-out=saved.tfplan",
            ],
            work,
            env,
        )
        plan_json = json.loads(_run([terraform_bin, "show", "-json", str(plan_file)], work, env))
        plan_bytes = plan_file.read_bytes()
        spec_bytes = canonical_json_bytes(request.model_dump(mode="json"))

        def artifact(name: str, kind: str, content: bytes) -> ArtifactRef:
            return ArtifactRef(
                id=f"{request.plan.plan_id}-r{request.plan.revision}-{name}",
                version="1",
                kind=kind,
                sha256=sha256_bytes(content),
                size_bytes=len(content),
            )

        manifest = PlanManifest(
            contract_version="1",
            digest_format="cmp-json-v1",
            migration_id=request.migration_id,
            plan=request.plan,
            target=CloudIdentity(
                provider="GCP",
                environment_id=request.environment_id,
                account_id=request.project_id,
                region=request.region,
                state_ref=request.state_ref,
            ),
            scope="INFRASTRUCTURE",
            artifacts={
                "spec": artifact("spec", "SPEC", spec_bytes),
                "terraform_plan": artifact("terraform-plan", "TERRAFORM_PLAN", plan_bytes),
            },
        )
        preview = preview_from_plan(plan_json, request, manifest_digest(manifest))
        if mode != "S3":
            preview = preview.model_copy(
                update={
                    "errors": [
                        ValidationIssue(
                            code="LOCAL_STATE_UNAPPROVABLE",
                            path="terraform_plan",
                            message="Local State Plan lacks central S3 State verification.",
                        )
                    ]
                }
            )
        target.mkdir(mode=0o700)
        for filename, content in (
            ("spec.json", spec_bytes),
            ("saved.tfplan", plan_bytes),
            ("manifest.json", manifest.model_dump_json().encode()),
            ("preview.json", preview.model_dump_json().encode()),
        ):
            destination = target / filename
            destination.write_bytes(content)
            os.chmod(destination, 0o600)
        evidence = {
            "plan_id": request.plan.plan_id,
            "revision": request.plan.revision,
            "template_version": request.plan.template_version,
            "template_digest": sha256_bytes(canonical_json_bytes(template_digests)),
            "provider_lock_digest": sha256_bytes(lock.read_bytes()),
            "state_ref": request.state_ref.model_dump(mode="json"),
            "state_bucket": request.state_bucket,
            "state_key": request.state_key,
            "state_region": request.state_region,
            "manifest_digest": manifest_digest(manifest),
            "plan_mode": mode,
        }
        (target / "evidence.json").write_bytes(canonical_json_bytes(evidence))
        os.chmod(target / "evidence.json", 0o600)
        return manifest, preview
    finally:
        shutil.rmtree(work)
