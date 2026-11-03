"""AWS infrastructure-only Terraform Plan and allowlisted Preview boundary."""

import json
import os
import re
import shutil
import subprocess
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
_RESOURCE_TYPES = {
    "aws_vpc": ("NETWORK", "VPC"),
    "aws_subnet": ("NETWORK", "Subnet"),
    "aws_internet_gateway": ("NETWORK", "Internet gateway"),
    "aws_route_table": ("NETWORK", "Route table"),
    "aws_route": ("NETWORK", "Route"),
    "aws_route_table_association": ("NETWORK", "Route association"),
    "aws_eip": ("NETWORK", "NAT elastic IP"),
    "aws_nat_gateway": ("NETWORK", "NAT gateway"),
    "aws_security_group": ("NETWORK", "Cluster security group"),
    "aws_vpc_security_group_egress_rule": ("NETWORK", "Cluster egress rule"),
    "aws_iam_role": ("CLUSTER", "EKS IAM role"),
    "aws_iam_role_policy_attachment": ("CLUSTER", "EKS IAM policy attachment"),
    "aws_eks_cluster": ("CLUSTER", "EKS cluster"),
    "aws_eks_node_group": ("NODE", "Managed node group"),
}
_ACTIONS = {
    "create": "CREATE",
    "update": "UPDATE",
    "delete": "DELETE",
    "read": "READ",
    "no-op": "NO_CHANGE",
}
BackendMode = Literal["S3", "LOCAL_VALIDATION"]


class AwsSubnet(Contract):
    cidr: Text
    zone: Text
    role: Literal["PUBLIC", "PRIVATE"]


class AwsPlanInput(Contract):
    """Explicit infrastructure input; no credential, workload or Secret bytes."""

    migration_id: Text
    plan: PlanRef
    environment_id: Text
    account_id: Annotated[str, Field(pattern=r"^[0-9]{12}$")]
    region: Annotated[str, Field(pattern=r"^[a-z]{2}(?:-[a-z]+)+-[0-9]$")]
    credential_ref: VersionRef
    state_ref: VersionRef
    state_bucket: Text | None = None
    state_key: Text
    cluster_name: Annotated[str, Field(pattern=_NAME)]
    kubernetes_version: Annotated[str, Field(pattern=r"^1\.[0-9]{2}$")]
    vpc_cidr: Text
    subnets: list[AwsSubnet] = Field(min_length=4)
    api_access_cidrs: list[Text] = Field(min_length=1)
    node_instance_type: Annotated[str, Field(pattern=r"^[a-z0-9]+\.[a-z0-9]+$")]
    node_count: int = Field(ge=1, le=20)
    node_disk_gib: int = Field(ge=20, le=1024)


def _reject(code: str, path: str, message: str) -> None:
    raise ProvisioningInputError([ValidationIssue(code=code, path=path, message=message)])


def _network(value: str, path: str) -> IPv4Network:
    try:
        result = ip_network(value, strict=True)
        if not isinstance(result, IPv4Network):
            raise ValueError("IPv6 is unsupported")  # noqa: TRY004
        return result
    except ValueError:
        _reject("INVALID_CIDR", path, "Expected a canonical IPv4 CIDR.")


def validate_aws_input(request: AwsPlanInput) -> None:
    for path, value in (
        ("plan.plan_id", request.plan.plan_id),
        ("environment_id", request.environment_id),
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            _reject("INVALID_IDENTIFIER", path, "Expected a safe opaque identifier.")
    vpc = _network(request.vpc_cidr, "vpc_cidr")
    if not 16 <= vpc.prefixlen <= 24:
        _reject("INVALID_TOPOLOGY", "vpc_cidr", "VPC CIDR must be between /16 and /24.")
    if request.plan.template_version != "aws-eks-v1":
        _reject(
            "UNSUPPORTED_TEMPLATE", "plan.template_version", "Unsupported AWS template version."
        )
    if request.state_key != f"cmp/{request.environment_id}/terraform.tfstate":
        _reject("INVALID_STATE_KEY", "state_key", "State key must be stable per environment.")
    if request.state_bucket is not None and (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", request.state_bucket)
        or ".." in request.state_bucket
    ):
        _reject("INVALID_STATE_BUCKET", "state_bucket", "Expected an existing S3 bucket name.")
    zones: dict[str, set[str]] = {}
    networks = []
    for index, subnet in enumerate(request.subnets):
        path = f"subnets.{index}"
        cidr = _network(subnet.cidr, f"{path}.cidr")
        if cidr.prefixlen > 27 or not cidr.subnet_of(vpc):
            _reject("INVALID_TOPOLOGY", path, "Subnet must be within VPC and at least /27.")
        if (
            not subnet.zone.startswith(request.region)
            or len(subnet.zone) != len(request.region) + 1
        ):
            _reject("INVALID_TOPOLOGY", f"{path}.zone", "Subnet AZ must match the region.")
        if any(cidr.overlaps(other) for other in networks):
            _reject("CIDR_OVERLAP", f"{path}.cidr", "Subnets overlap.")
        networks.append(cidr)
        zones.setdefault(subnet.zone, set()).add(subnet.role)
    if len(zones) < 2 or any(roles != {"PUBLIC", "PRIVATE"} for roles in zones.values()):
        _reject("INVALID_TOPOLOGY", "subnets", "At least two AZs need public and private subnets.")
    for index, cidr in enumerate(request.api_access_cidrs):
        network = _network(cidr, f"api_access_cidrs.{index}")
        if network.prefixlen == 0:
            _reject(
                "PUBLIC_API_EXPOSURE", f"api_access_cidrs.{index}", "Restrict API access CIDRs."
            )


def terraform_variables(request: AwsPlanInput) -> dict:
    """Map reviewed input to the fixed Terraform root; no credential material."""
    validate_aws_input(request)
    groups = {"PUBLIC": {}, "PRIVATE": {}}
    for subnet in request.subnets:
        groups[subnet.role][subnet.zone] = {"cidr": subnet.cidr, "zone": subnet.zone}
    if sum(len(group) for group in groups.values()) != len(request.subnets):
        _reject("INVALID_TOPOLOGY", "subnets", "Only one subnet per role and AZ is supported.")
    return {
        "region": request.region,
        "account_id": request.account_id,
        "name": request.cluster_name,
        "kubernetes_version": request.kubernetes_version,
        "vpc_cidr": request.vpc_cidr,
        "public_subnets": groups["PUBLIC"],
        "private_subnets": groups["PRIVATE"],
        "api_access_cidrs": request.api_access_cidrs,
        "node_instance_type": request.node_instance_type,
        "node_count": request.node_count,
        "node_disk_gib": request.node_disk_gib,
    }


def preview_from_plan(plan_json: dict, request: AwsPlanInput, digest: str) -> Preview:
    """Derive actions from Terraform while discarding every after/before/sensitive value."""
    counts: dict[tuple[str, str, str], int] = {}
    for change in plan_json.get("resource_changes", []):
        if change.get("mode") == "data":
            continue
        kind = change.get("type")
        if change.get("mode") != "managed" or kind not in _RESOURCE_TYPES:
            _reject("UNREVIEWED_PLAN", "terraform_plan", "Plan contains an unexpected resource.")
        for action in change.get("change", {}).get("actions", []):
            if action not in _ACTIONS:
                _reject("UNREVIEWED_PLAN", "terraform_plan", "Plan contains an unknown action.")
            if action in {"update", "delete"}:
                _reject(
                    "DESTRUCTIVE_PLAN",
                    "terraform_plan",
                    "New-network Plan cannot change or remove existing resources.",
                )
            resource_type, name = _RESOURCE_TYPES[kind]
            key = resource_type, _ACTIONS[action], name
            counts[key] = counts.get(key, 0) + 1
    required = {
        ("NETWORK", "CREATE", "VPC"),
        ("CLUSTER", "CREATE", "EKS cluster"),
        ("NODE", "CREATE", "Managed node group"),
    }
    if not required.issubset(counts):
        _reject("INCOMPLETE_NEW_PLAN", "terraform_plan", "New VPC, EKS and nodes must be created.")
    subnet_summary = "; ".join(
        f"{subnet.role.lower()} {subnet.zone} {subnet.cidr}" for subnet in request.subnets
    )
    display = {
        "VPC": request.vpc_cidr,
        "Subnet": subnet_summary,
        "Route": "public 0.0.0.0/0 via IGW; private 0.0.0.0/0 via single NAT",
        "Cluster security group": "No explicit ingress; outbound 0.0.0.0/0",
        "EKS cluster": f"Kubernetes {request.kubernetes_version}; public API {', '.join(request.api_access_cidrs)}; private API enabled",
        "Managed node group": f"{request.node_instance_type}; {request.node_count} nodes; {request.node_disk_gib} GiB each; private subnets",
    }
    changes = [
        ResourceChange(
            resource_type=kind, action=action, name=name, quantity=count, size=display.get(name)
        )
        for (kind, action, name), count in sorted(counts.items())
    ]
    defaults = [
        ResourceChange(
            resource_type="NETWORK",
            action="CREATE",
            name="One NAT gateway for private subnet egress",
            quantity=1,
        ),
        ResourceChange(
            resource_type="CLUSTER", action="CREATE", name="Private EKS API endpoint enabled"
        ),
        ResourceChange(
            resource_type="INGRESS",
            action="NO_CHANGE",
            name="Public ALB subnet tags prepared; ALB not deployed",
        ),
    ]
    warnings = [
        ValidationIssue(
            code="ALB_NOT_DEPLOYED",
            path="external_access",
            message="ALB controller, Ingress, TLS and public service remain outside this Plan.",
        ),
        ValidationIssue(
            code="SINGLE_NAT_AZ",
            path="network",
            message="One NAT gateway provides private subnet egress and is an AZ dependency.",
        ),
    ]
    return Preview(
        contract_version="1",
        manifest_digest=digest,
        changes=changes,
        applied_defaults=defaults,
        warnings=warnings,
    )


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> bytes:
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, check=False, timeout=600
        )
    except (OSError, subprocess.TimeoutExpired):
        _reject("TOOL_UNAVAILABLE", "terraform", "Terraform or AWS CLI could not complete.")
    if result.returncode:
        # Terraform stdout/stderr can contain credentials and sensitive state values.
        _reject("PLAN_COMMAND_FAILED", "terraform", f"Terraform command failed at {args[1]}.")
    return result.stdout


def plan_aws_infrastructure(
    request: AwsPlanInput,
    *,
    output_dir: Path,
    terraform_bin: str = "terraform",
    aws_profile: str,
    template_dir: Path | None = None,
    backend_mode: BackendMode = "S3",
) -> tuple[PlanManifest, Preview]:
    """Run a saved Plan; local validation is never an approvable production Plan."""
    variables = terraform_variables(request)
    if backend_mode not in {"S3", "LOCAL_VALIDATION"}:
        _reject("UNSUPPORTED_BACKEND", "backend_mode", "Unknown Plan backend mode.")
    if backend_mode == "S3" and request.state_bucket is None:
        _reject("MISSING_STATE_BUCKET", "state_bucket", "S3 Plan requires an existing bucket.")
    if backend_mode == "LOCAL_VALIDATION" and request.state_bucket is not None:
        _reject(
            "UNEXPECTED_STATE_BUCKET",
            "state_bucket",
            "Local validation cannot name a State bucket.",
        )
    if not aws_profile or aws_profile == "default":
        _reject("MISSING_PROFILE", "aws_profile", "An explicit named AWS profile is required.")
    root = template_dir or Path(__file__).resolve().parents[4] / "infra/terraform/aws"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    target = output_dir / f"{request.plan.plan_id}-r{request.plan.revision}"
    if target.exists():
        _reject("IMMUTABLE_PLAN_EXISTS", "output_dir", "Plan revision already exists.")
    work = Path(mkdtemp(prefix="aws-plan-", dir=output_dir))
    os.chmod(work, 0o700)
    try:
        template_digests = {}
        for source in root.glob("*.tf"):
            shutil.copy2(source, work / source.name)
            template_digests[source.name] = sha256_bytes(source.read_bytes())
        if backend_mode == "LOCAL_VALIDATION":
            versions_file = work / "versions.tf"
            contents = versions_file.read_text()
            backend_block = '  backend "s3" {}\n'
            if contents.count(backend_block) != 1:
                _reject("UNREVIEWED_TEMPLATE", "terraform", "Expected one S3 backend block.")
            versions_file.write_text(contents.replace(backend_block, ""))
        effective_template_digests = {
            path.name: sha256_bytes(path.read_bytes()) for path in work.glob("*.tf")
        }
        lock = root / ".terraform.lock.hcl"
        if not lock.exists():
            _reject(
                "MISSING_PROVIDER_LOCK", "terraform", "A reviewed provider lockfile is required."
            )
        shutil.copy2(lock, work / lock.name)
        var_file = work / "input.auto.tfvars.json"
        var_file.write_bytes(canonical_json_bytes(variables))
        os.chmod(var_file, 0o600)
        env = dict(
            os.environ,
            AWS_PROFILE=aws_profile,
            AWS_DEFAULT_PROFILE=aws_profile,
            TF_IN_AUTOMATION="1",
            TF_INPUT="0",
        )
        env.pop("TF_LOG", None)
        identity = _run(
            ["aws", "sts", "get-caller-identity", "--profile", aws_profile, "--output", "json"],
            work,
            env,
        )
        try:
            account = json.loads(identity)["Account"]
        except (ValueError, KeyError, TypeError):
            _reject("ACCOUNT_CHECK_FAILED", "account_id", "AWS account identity is unavailable.")
        if account != request.account_id:
            _reject("ACCOUNT_MISMATCH", "account_id", "AWS profile is for another account.")
        init_args = [terraform_bin, "init", "-input=false", "-lockfile=readonly"]
        if backend_mode == "S3":
            init_args.extend(
                [
                    f"-backend-config=bucket={request.state_bucket}",
                    f"-backend-config=key={request.state_key}",
                    f"-backend-config=region={request.region}",
                    "-backend-config=encrypt=true",
                    "-backend-config=use_lockfile=true",
                ]
            )
        else:
            init_args.append("-backend=false")
        _run(init_args, work, env)
        _run([terraform_bin, "validate", "-no-color"], work, env)
        plan_file = work / "saved.tfplan"
        _run(
            [terraform_bin, "plan", "-input=false", "-lock=true", "-no-color", "-out=saved.tfplan"],
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
                provider="AWS",
                environment_id=request.environment_id,
                account_id=request.account_id,
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
        if backend_mode == "LOCAL_VALIDATION":
            preview.pending_decisions.append(
                ValidationIssue(
                    code="LOCAL_STATE_VALIDATION_ONLY",
                    path="terraform_plan",
                    message="Generate a new S3-backed Plan before production approval.",
                )
            )
        target.mkdir(mode=0o700)
        (target / "spec.json").write_bytes(spec_bytes)
        (target / "saved.tfplan").write_bytes(plan_bytes)
        os.chmod(target / "spec.json", 0o600)
        os.chmod(target / "saved.tfplan", 0o600)
        (target / "manifest.json").write_text(manifest.model_dump_json())
        (target / "preview.json").write_text(preview.model_dump_json())
        evidence = {
            "plan_id": request.plan.plan_id,
            "revision": request.plan.revision,
            "template_version": request.plan.template_version,
            "template_digest": sha256_bytes(canonical_json_bytes(template_digests)),
            "effective_template_digest": sha256_bytes(
                canonical_json_bytes(effective_template_digests)
            ),
            "provider_lock_digest": sha256_bytes(lock.read_bytes()),
            "backend_mode": backend_mode,
            "state_ref": request.state_ref.model_dump(mode="json"),
            "state_bucket": request.state_bucket,
            "state_key": request.state_key,
            "manifest_digest": manifest_digest(manifest),
        }
        (target / "evidence.json").write_bytes(canonical_json_bytes(evidence))
        for filename in ("manifest.json", "preview.json", "evidence.json"):
            os.chmod(target / filename, 0o600)
        return manifest, preview
    finally:
        shutil.rmtree(work)
