"""AWS 인프라 Terraform Plan과 허용 목록 기반 Preview 생성 경계."""

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
from cmp_providers.resources import resource_root

_NAME = r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$"
_RESOURCE_TYPES = {
    "aws_vpc": ("NETWORK", "VPC"),
    "aws_subnet": ("NETWORK", "서브넷"),
    "aws_internet_gateway": ("NETWORK", "인터넷 게이트웨이"),
    "aws_route_table": ("NETWORK", "라우팅 테이블"),
    "aws_route": ("NETWORK", "라우트"),
    "aws_route_table_association": ("NETWORK", "라우팅 테이블 연결"),
    "aws_eip": ("NETWORK", "NAT 탄력적 IP"),
    "aws_nat_gateway": ("NETWORK", "NAT 게이트웨이"),
    "aws_security_group": ("NETWORK", "클러스터 보안 그룹"),
    "aws_vpc_security_group_egress_rule": ("NETWORK", "클러스터 아웃바운드 규칙"),
    "aws_iam_role": ("CLUSTER", "EKS IAM 역할"),
    "aws_iam_role_policy_attachment": ("CLUSTER", "EKS IAM 정책 연결"),
    "aws_eks_cluster": ("CLUSTER", "EKS 클러스터"),
    "aws_eks_node_group": ("NODE", "관리형 노드 그룹"),
    # Ticket 08 (E6): IRSA prerequisites for EBS CSI and AWS Load Balancer Controller.
    "aws_iam_openid_connect_provider": ("CLUSTER", "IAM OIDC 공급자"),
    "aws_eks_addon": ("CLUSTER", "EKS 애드온(EBS CSI·VPC CNI)"),
    "aws_iam_policy": ("CLUSTER", "Load Balancer Controller IAM 정책"),
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
    """Credential·워크로드·Secret 원문을 받지 않는 명시적 인프라 입력."""

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
            raise ValueError("IPv6는 지원하지 않습니다")  # noqa: TRY004
        return result
    except ValueError:
        _reject("INVALID_CIDR", path, "정규화된 IPv4 CIDR가 필요합니다.")


def validate_aws_input(request: AwsPlanInput) -> None:
    for path, value in (
        ("plan.plan_id", request.plan.plan_id),
        ("environment_id", request.environment_id),
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            _reject("INVALID_IDENTIFIER", path, "안전한 불투명 식별자가 필요합니다.")
    vpc = _network(request.vpc_cidr, "vpc_cidr")
    if not 16 <= vpc.prefixlen <= 24:
        _reject("INVALID_TOPOLOGY", "vpc_cidr", "VPC CIDR는 /16에서 /24 사이여야 합니다.")
    if request.plan.template_version != "aws-eks-v3":
        _reject(
            "UNSUPPORTED_TEMPLATE", "plan.template_version", "지원하지 않는 AWS 템플릿 버전입니다."
        )
    if request.state_key != f"cmp/{request.environment_id}/terraform.tfstate":
        _reject("INVALID_STATE_KEY", "state_key", "State key는 환경별로 고정되어야 합니다.")
    if request.state_bucket is not None and (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", request.state_bucket)
        or ".." in request.state_bucket
    ):
        _reject("INVALID_STATE_BUCKET", "state_bucket", "기존 S3 버킷 이름이 필요합니다.")
    zones: dict[str, set[str]] = {}
    networks = []
    for index, subnet in enumerate(request.subnets):
        path = f"subnets.{index}"
        cidr = _network(subnet.cidr, f"{path}.cidr")
        if cidr.prefixlen > 27 or not cidr.subnet_of(vpc):
            _reject(
                "INVALID_TOPOLOGY", path, "서브넷은 VPC 안에 있어야 하며 /27 이상이어야 합니다."
            )
        if (
            not subnet.zone.startswith(request.region)
            or len(subnet.zone) != len(request.region) + 1
        ):
            _reject("INVALID_TOPOLOGY", f"{path}.zone", "서브넷 AZ가 대상 리전과 일치해야 합니다.")
        if any(cidr.overlaps(other) for other in networks):
            _reject("CIDR_OVERLAP", f"{path}.cidr", "서브넷 CIDR가 겹칩니다.")
        networks.append(cidr)
        zones.setdefault(subnet.zone, set()).add(subnet.role)
    if len(zones) < 2 or any(roles != {"PUBLIC", "PRIVATE"} for roles in zones.values()):
        _reject("INVALID_TOPOLOGY", "subnets", "최소 2개 AZ에 공개·비공개 서브넷이 필요합니다.")
    for index, cidr in enumerate(request.api_access_cidrs):
        network = _network(cidr, f"api_access_cidrs.{index}")
        if network.prefixlen == 0:
            _reject(
                "PUBLIC_API_EXPOSURE",
                f"api_access_cidrs.{index}",
                "API 접근 CIDR를 제한해야 합니다.",
            )


def terraform_variables(request: AwsPlanInput) -> dict:
    """검토한 입력을 고정 Terraform 구성의 변수로 변환한다."""
    validate_aws_input(request)
    groups = {"PUBLIC": {}, "PRIVATE": {}}
    for subnet in request.subnets:
        groups[subnet.role][subnet.zone] = {"cidr": subnet.cidr, "zone": subnet.zone}
    if sum(len(group) for group in groups.values()) != len(request.subnets):
        _reject("INVALID_TOPOLOGY", "subnets", "역할·AZ별 서브넷 하나만 지원합니다.")
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
    """Terraform 변경 행위만 읽고 실제 값과 민감값은 버린다."""
    counts: dict[tuple[str, str, str], int] = {}
    for change in plan_json.get("resource_changes", []):
        if change.get("mode") == "data":
            continue
        kind = change.get("type")
        if change.get("mode") != "managed" or kind not in _RESOURCE_TYPES:
            _reject("UNREVIEWED_PLAN", "terraform_plan", "Plan에 예상하지 못한 자원이 있습니다.")
        for action in change.get("change", {}).get("actions", []):
            if action not in _ACTIONS:
                _reject("UNREVIEWED_PLAN", "terraform_plan", "Plan에 알 수 없는 행위가 있습니다.")
            if action in {"update", "delete"}:
                _reject(
                    "DESTRUCTIVE_PLAN",
                    "terraform_plan",
                    "신규 네트워크 Plan은 기존 자원을 변경하거나 삭제할 수 없습니다.",
                )
            resource_type, name = _RESOURCE_TYPES[kind]
            key = resource_type, _ACTIONS[action], name
            counts[key] = counts.get(key, 0) + 1
    # Ticket 14 F9: a recovery Revision over this environment's own State may keep resources
    # already created by an earlier (possibly lost) Apply. They must be unchanged (no-op); any
    # update/delete is still rejected above, and the full topology must still be present.
    present = {(kind, name) for kind, action, name in counts if action in ("CREATE", "NO_CHANGE")}
    required = {("NETWORK", "VPC"), ("CLUSTER", "EKS 클러스터"), ("NODE", "관리형 노드 그룹")}
    if not required.issubset(present):
        _reject("INCOMPLETE_NEW_PLAN", "terraform_plan", "신규 VPC·EKS·Node 계획이 필요합니다.")
    continuation = any(action == "NO_CHANGE" for _, action, _ in counts)
    subnet_summary = "; ".join(
        f"{'공개' if subnet.role == 'PUBLIC' else '비공개'} {subnet.zone} {subnet.cidr}"
        for subnet in request.subnets
    )
    display = {
        "VPC": request.vpc_cidr,
        "서브넷": subnet_summary,
        "라우트": "공개 0.0.0.0/0 → IGW; 비공개 0.0.0.0/0 → 단일 NAT",
        "클러스터 보안 그룹": "명시적 인바운드 없음; 아웃바운드 0.0.0.0/0",
        "EKS 클러스터": f"Kubernetes {request.kubernetes_version}; 공개 API {', '.join(request.api_access_cidrs)}; 사설 API 활성화",
        "관리형 노드 그룹": f"{request.node_instance_type}; 노드 {request.node_count}개; 각 {request.node_disk_gib} GiB; 비공개 서브넷",
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
            name="비공개 서브넷 아웃바운드용 NAT 게이트웨이 1개",
            quantity=1,
        ),
        ResourceChange(
            resource_type="CLUSTER", action="CREATE", name="EKS 사설 API Endpoint 활성화"
        ),
        ResourceChange(  # ticket 26
            resource_type="NETWORK",
            action="CREATE",
            name="VPC CNI NetworkPolicy 강제(enableNetworkPolicy=true)",
        ),
        ResourceChange(
            resource_type="INGRESS",
            action="NO_CHANGE",
            name="공개 ALB용 서브넷 태그 준비; ALB 미배포",
        ),
    ]
    warnings = [
        ValidationIssue(
            code="ALB_NOT_DEPLOYED",
            path="external_access",
            message=(
                "Load Balancer Controller IAM 역할만 준비합니다. Controller 설치·Ingress·TLS·"
                "공개 서비스는 이 Plan의 범위 밖입니다(09)."
            ),
        ),
        ValidationIssue(
            code="SINGLE_NAT_AZ",
            path="network",
            message="단일 NAT 게이트웨이에 비공개 서브넷 아웃바운드가 의존합니다.",
        ),
    ]
    if continuation:
        warnings.append(
            ValidationIssue(
                code="RECOVERY_CONTINUATION",
                path="terraform_plan",
                message=(
                    "이 환경의 State에 이전 Apply가 만든 자원이 있어 변경 없이 유지합니다. "
                    "부족한 자원만 생성합니다(14 F9)."
                ),
            )
        )
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
        _reject("TOOL_UNAVAILABLE", "terraform", "Terraform 또는 AWS CLI를 완료하지 못했습니다.")
    if result.returncode:
        # Terraform 출력에는 자격 증명이나 민감한 State 값이 있을 수 있다.
        _reject("PLAN_COMMAND_FAILED", "terraform", f"Terraform 명령 {args[1]}이 실패했습니다.")
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
    """저장 Plan을 실행한다. 로컬 검증 Plan은 운영 승인이 불가능하다."""
    variables = terraform_variables(request)
    if backend_mode not in {"S3", "LOCAL_VALIDATION"}:
        _reject("UNSUPPORTED_BACKEND", "backend_mode", "알 수 없는 Plan Backend 모드입니다.")
    if backend_mode == "S3" and request.state_bucket is None:
        _reject("MISSING_STATE_BUCKET", "state_bucket", "S3 Plan에는 기존 버킷이 필요합니다.")
    if backend_mode == "LOCAL_VALIDATION" and request.state_bucket is not None:
        _reject(
            "UNEXPECTED_STATE_BUCKET",
            "state_bucket",
            "로컬 검증에서는 State 버킷을 지정할 수 없습니다.",
        )
    if not aws_profile or aws_profile == "default":
        _reject("MISSING_PROFILE", "aws_profile", "이름이 지정된 AWS 프로필이 필요합니다.")
    root = template_dir or resource_root() / "infra/terraform/aws"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    target = output_dir / f"{request.plan.plan_id}-r{request.plan.revision}"
    if target.exists():
        _reject("IMMUTABLE_PLAN_EXISTS", "output_dir", "해당 Plan Revision이 이미 존재합니다.")
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
                _reject("UNREVIEWED_TEMPLATE", "terraform", "S3 Backend 블록 하나가 필요합니다.")
            versions_file.write_text(contents.replace(backend_block, ""))
        effective_template_digests = {
            path.name: sha256_bytes(path.read_bytes()) for path in work.glob("*.tf")
        }
        lock = root / ".terraform.lock.hcl"
        if not lock.exists():
            _reject("MISSING_PROVIDER_LOCK", "terraform", "검토된 Provider lockfile이 필요합니다.")
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
            _reject("ACCOUNT_CHECK_FAILED", "account_id", "AWS 계정 식별을 확인할 수 없습니다.")
        if account != request.account_id:
            _reject("ACCOUNT_MISMATCH", "account_id", "AWS 프로필이 다른 계정을 가리킵니다.")
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
                    message="운영 승인 전에 S3 State를 사용하는 새 Plan을 생성해야 합니다.",
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
